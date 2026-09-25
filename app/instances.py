"""Multi-Server-Verwaltung: unabhängige Server-Instanzen mit eigenen Verzeichnissen.

Jede Instanz bekommt ein eigenes Unterverzeichnis unter INSTANCES_DIR:
    <INSTANCES_DIR>/<id>/
        instance.json        Metadaten (Name, Loader, Version, Port, Status)
        eula.txt             EULA-Zustimmung
        server.properties    Basis-Konfiguration
        mods/                Mod-/Modpack-Dateien
        packs/               heruntergeladene .mrpack-Archive
"""
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from fastapi import HTTPException

from .config import ALLOWED_LOADERS, settings
from .security import validate_identifier

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,63}$")
_MEMORY_RE = re.compile(r"^(?:[1-9]\d{0,3})[GgMm]$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,31}$")
_META_FILE = "instance.json"
_MAX_TAGS = 8
_LOCK = threading.Lock()
_MAX_JVM_OPTS = 2000
# Beim Klonen übersprungene Einträge: Metadaten (neu erzeugt) und
# Modpack-Archive (nur Installations-Quellen, spart doppelten Platz)
_CLONE_SKIP = shutil.ignore_patterns(_META_FILE, _META_FILE + ".tmp", "packs")

# Container-Konfiguration wird von runtime.py gelesen
SERVER_PROPERTIES_TEMPLATE = """# Erstellt vom Minecraft-Dashboard
server-port=25565
motd={name} (verwaltet vom Dashboard)
max-players=20
online-mode=true
view-distance=8
spawn-protection=8
"""


def _root() -> Path:
    return settings.instances_dir


def instance_dir(instance_id: str):
    """Verzeichnis einer Instanz; schützt vor Pfadmanipulation."""
    validate_identifier(instance_id, "Instanz-ID")
    path = (_root() / instance_id).resolve()
    if path.parent != _root().resolve():
        raise HTTPException(status_code=400, detail="Pfadmanipulation erkannt")
    return path


def _meta_path(instance_id: str):
    return instance_dir(instance_id) / _META_FILE


def _load_meta(instance_id: str) -> dict:
    try:
        data = json.loads(_meta_path(instance_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Instanz nicht gefunden")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Instanz-Metadaten unlesbar: {exc}")
    except ValueError:
        raise HTTPException(status_code=500, detail="Instanz-Metadaten beschädigt")
    if not isinstance(data, dict) or not data.get("id"):
        raise HTTPException(status_code=500, detail="Instanz-Metadaten beschädigt")
    return data


def _save_meta(instance: dict) -> None:
    directory = instance_dir(instance["id"])
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / (_META_FILE + ".tmp")
    try:
        tmp.write_text(json.dumps(instance, indent=2), encoding="utf-8")
        os.replace(tmp, directory / _META_FILE)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Instanz-Metadaten nicht schreibbar: {exc}")


def _scan() -> list:
    """Liest alle vorhandenen Instanzen aus dem Dateisystem."""
    try:
        entries = sorted(_root().iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Instanz-Ordner nicht lesbar: {exc}")
    instances = []
    for entry in entries:
        if not (entry / _META_FILE).is_file():
            continue
        try:
            instances.append(json.loads((entry / _META_FILE).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue  # beschädigte Instanzen überspringen statt komplett zu scheitern
    return instances


def _used_port_sets(existing: list) -> tuple:
    """Belegte Spiel- und RCON-Ports aller Instanzen (RCON = port+1000)."""
    game, rcon = set(), set()
    for i in existing:
        try:
            game.add(int(i["port"]))
            rcon.add(int(i.get("rcon_port") or int(i["port"]) + 1000))
        except (KeyError, TypeError, ValueError):
            continue
    return game, rcon


def _validate_port(port: int | None, used_game: set, used_rcon: set) -> int:
    if port is None:
        base = settings.instances_port_base
        for candidate in range(base, base + 4000):
            if candidate in used_game or candidate in used_rcon:
                continue
            if candidate + 1000 in used_game or candidate + 1000 in used_rcon:
                continue  # RCON-Port (port+1000) kollidiert
            return candidate
        raise HTTPException(status_code=500,
                            detail="Kein freier Port im Instanz-Port-Bereich mehr")
    if not (1024 <= port <= 65535):
        raise HTTPException(status_code=400, detail="Port muss zwischen 1024 und 65535 liegen")
    if port + 1000 > 65535:
        raise HTTPException(status_code=400,
                            detail="Port zu hoch — der RCON-Port (Port + 1000) läge über 65535")
    if port in used_game or port in used_rcon:
        raise HTTPException(status_code=409, detail=f"Port {port} ist bereits belegt")
    if port + 1000 in used_game or port + 1000 in used_rcon:
        raise HTTPException(status_code=409,
                            detail=f"RCON-Port {port + 1000} (Port {port} + 1000) ist bereits belegt")
    return port


def _validate_memory(memory: str | None) -> str | None:
    if memory is None:
        return None
    memory = memory.strip()
    if not _MEMORY_RE.match(memory):
        raise HTTPException(status_code=400, detail="RAM muss z. B. '2G' oder '512M' sein")
    return memory


def _validate_jvm_opts(value: str | None) -> str | None:
    """JVM-Zusatzoptionen (JVM_OPTS): einzeilig, keine Steuerzeichen."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > _MAX_JVM_OPTS:
        raise HTTPException(status_code=400,
                            detail=f"JVM-Flags zu lang (max. {_MAX_JVM_OPTS} Zeichen)")
    if any(ord(ch) < 32 for ch in value):
        raise HTTPException(status_code=400,
                            detail="JVM-Flags dürfen keine Zeilenumbrüche/Steuerzeichen enthalten")
    return value


def _validate_instance_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400,
                            detail="Name: 1-64 Zeichen, Buchstaben/Zahlen/Leerzeichen . _ -")
    return name


def _validate_tags(tags: list | None) -> list[str]:
    """Tag-Liste prüfen (Gruppen-Zugehörigkeit): trimmen, Dedupe
    (case-insensitive, erste Schreibweise gewinnt), max 8 Tags."""
    if tags is None:
        return []
    if not isinstance(tags, list):
        raise HTTPException(status_code=400, detail="Tags müssen eine Liste sein")
    out: list[str] = []
    seen: set[str] = set()
    for item in tags:
        tag = str(item or "").strip()
        if not tag:
            continue
        if not _TAG_RE.match(tag):
            raise HTTPException(
                status_code=400,
                detail=(f"Ungültiger Tag '{tag[:32]}': 1-32 Zeichen, "
                        "Buchstaben/Zahlen/Leerzeichen _ -"))
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    if len(out) > _MAX_TAGS:
        raise HTTPException(status_code=400,
                            detail=f"Maximal {_MAX_TAGS} Tags pro Instanz")
    return out


def unique_instance_name(base: str) -> str:
    """Freien Instanznamen ableiten (Anhängen von ' 2', ' 3', …)."""
    base = base[:64]
    existing = {i["name"].lower() for i in _scan()}
    name, suffix = base, 2
    while name.lower() in existing:
        if suffix > 50:
            raise HTTPException(status_code=409,
                                detail="Kann keinen freien Instanznamen ableiten")
        tail = f" {suffix}"
        name = base[:64 - len(tail)].rstrip(" .-") + tail
        suffix += 1
    return name


def create_instance(name: str, loader: str, game_version: str,
                    loader_version: str | None = None,
                    port: int | None = None,
                    memory: str | None = None,
                    accept_eula: bool = False,
                    tags: list | None = None) -> dict:
    """Erstellt eine neue, vollständig getrennte Server-Instanz."""
    if not accept_eula:
        raise HTTPException(status_code=400,
                            detail="Die Minecraft-EULA muss akzeptiert werden")
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400,
                            detail="Name: 1-64 Zeichen, Buchstaben/Zahlen/Leerzeichen . _ -")
    loader = (loader or "").strip().lower()
    if loader not in ALLOWED_LOADERS:
        raise HTTPException(status_code=400,
                            detail=f"Loader muss einer von {', '.join(ALLOWED_LOADERS)} sein")
    validate_identifier(game_version, "Minecraft-Version")
    if loader_version is not None:
        loader_version = loader_version.strip()
        if loader_version:
            validate_identifier(loader_version, "Loader-Version")
        else:
            loader_version = None
    memory = _validate_memory(memory)

    with _LOCK:
        existing = _scan()
        if any(i["name"].lower() == name.lower() for i in existing):
            raise HTTPException(status_code=409,
                                detail=f"Instanz-Name '{name}' ist bereits vergeben")
        used_game, used_rcon = _used_port_sets(existing)
        port = _validate_port(port, used_game, used_rcon)
        instance = {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "loader": loader,
            "game_version": game_version,
            "loader_version": loader_version,
            "port": port,
            "rcon_port": port + 1000,
            "memory": memory,
            "created_at": int(time.time()),
            "status": "stopped",
            "error": None,
            "modpack": None,
            "tags": _validate_tags(tags),
        }
        directory = instance_dir(str(instance["id"]))
        try:
            (directory / "mods").mkdir(parents=True, exist_ok=True)
            (directory / "packs").mkdir(parents=True, exist_ok=True)
            (directory / "eula.txt").write_text("eula=true\n", encoding="utf-8")
            (directory / "server.properties").write_text(
                SERVER_PROPERTIES_TEMPLATE.format(name=name), encoding="utf-8")
        except OSError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            raise HTTPException(status_code=500,
                                detail=f"Instanz-Ordner nicht erstellbar: {exc}")
        try:
            _save_meta(instance)
        except HTTPException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
    return instance


def list_instances() -> list:
    """Alle Instanzen, älteste zuerst."""
    instances = _scan()
    instances.sort(key=lambda i: (i.get("created_at") or 0, i.get("id") or ""))
    return instances


def get_instance(instance_id: str) -> dict:
    return _load_meta(instance_id)


def update_instance(instance: dict) -> None:
    """Schreibt geänderte Metadaten zurück (Status, Modpack etc.)."""
    _save_meta(instance)


def set_status(instance_id: str, status: str, error: str | None = None) -> None:
    """Setzt den Verwaltungsstatus einer Instanz (stopped/starting/running/error)."""
    if status not in ("stopped", "starting", "running", "error"):
        raise ValueError(f"Unbekannter Status: {status}")
    instance = _load_meta(instance_id)
    instance["status"] = status
    instance["error"] = error
    _save_meta(instance)


def update_settings(instance_id: str, *, name=None, memory=None,
                    jvm_opts=None, use_aikar=None,
                    tags=None, port=None) -> dict:
    """Ändert Instanz-Einstellungen (Name, RAM, JVM-Flags, Tags, Port).
    Nur übergebene Felder werden geändert; JVM-/Port-Änderungen wirken
    beim nächsten (Neu-)Start. Port nur bei gestoppter Instanz änderbar."""
    instance = _load_meta(instance_id)
    changed = []
    if name is not None:
        name = _validate_instance_name(name)
        with _LOCK:
            if any(i["name"].lower() == name.lower() and i["id"] != instance_id
                   for i in _scan()):
                raise HTTPException(status_code=409,
                                    detail=f"Instanz-Name '{name}' ist bereits vergeben")
            instance["name"] = name
        changed.append("name")
    if memory is not None:
        instance["memory"] = _validate_memory(memory)
        changed.append("memory")
    if jvm_opts is not None:
        instance["jvm_opts"] = _validate_jvm_opts(jvm_opts)
        changed.append("jvm_opts")
    if use_aikar is not None:
        instance["use_aikar"] = bool(use_aikar)
        changed.append("use_aikar")
    if tags is not None:
        instance["tags"] = _validate_tags(tags)
        changed.append("tags")
    port_saved = False
    if port is not None:
        try:
            new_port = int(port)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Port muss eine Zahl sein")
        try:
            current = int(instance.get("port") or 0)
        except (TypeError, ValueError):
            current = 0
        if new_port != current:
            from . import runtime  # lazy, vermeidet Import-Zirkel
            running = runtime.running_state(instance)
            if running is None:
                raise HTTPException(
                    status_code=503,
                    detail=("Container-Status nicht prüfbar (Docker nicht erreichbar) "
                            "— Port-Wechsel abgelehnt, bitte später erneut versuchen"))
            if running:
                raise HTTPException(
                    status_code=409,
                    detail=("Instanz läuft — bitte zuerst stoppen, "
                            "bevor der Port geändert wird"))
            # Validierung + Mutation + Persistenz atomar, damit kein paralleler
            # PATCH denselben Port bekommen kann (TOCTOU).
            with _LOCK:
                others = [i for i in _scan() if i["id"] != instance_id]
                used_game, used_rcon = _used_port_sets(others)
                _validate_port(new_port, used_game, used_rcon)
                instance["port"] = new_port
                instance["rcon_port"] = new_port + 1000
                _save_meta(instance)
            port_saved = True
            changed.append("port")
    if not port_saved:
        _save_meta(instance)
    return {"instance": instance, "changed": changed}


# ---------------------------------------------------------------------------
# Zeitplan (Scheduler): Auto-Start, geplanter Neustart, Backups, Update-Check
# ---------------------------------------------------------------------------

_SCHEDULE_SECTIONS = {
    "restart": ("enabled", "time", "warn_minutes"),
    "stop": ("enabled", "time", "warn_minutes"),
    "backup": ("enabled", "interval_hours", "keep"),
    "update_check": ("enabled", "interval_hours"),
}
_SCHEDULE_RANGES = {
    ("restart", "warn_minutes"): (0, 30, 5),
    ("stop", "warn_minutes"): (0, 30, 5),
    ("backup", "interval_hours"): (1, 168, 6),
    ("backup", "keep"): (1, 20, 5),
    ("update_check", "interval_hours"): (1, 168, 24),
}
_SCHEDULE_DEFAULTS = {
    "restart": {"time": "04:00"},
    "stop": {"time": "23:00"},
}


def _validate_schedule_section(section: str, data: object) -> dict:
    """Validiert einen Unterabschnitt des Zeitplans (nur bekannte Schlüssel)."""
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail=f"Zeitplan '{section}': Objekt erwartet")
    allowed = _SCHEDULE_SECTIONS[section]
    out: dict = {}
    for key, value in data.items():
        if key not in allowed:
            raise HTTPException(status_code=400,
                                detail=f"Zeitplan '{section}': unbekanntes Feld '{key}'")
        if key == "enabled":
            out[key] = bool(value)
        elif key == "time":
            text = str(value or "").strip()
            if not _TIME_RE.match(text):
                raise HTTPException(
                    status_code=400,
                    detail=f"Zeitplan '{section}': Zeit muss 'HH:MM' sein "
                           f"(z. B. '04:00')")
            out[key] = text
        else:
            lo, hi, _default = _SCHEDULE_RANGES[(section, key)]
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400,
                                    detail=f"Zeitplan '{section}.{key}' muss eine Zahl sein")
            if not lo <= number <= hi:
                raise HTTPException(status_code=400,
                                    detail=f"Zeitplan '{section}.{key}' muss zwischen "
                                           f"{lo} und {hi} liegen")
            out[key] = number
    return out


def _validate_schedule(data: object) -> dict:
    """Validiert Schedule-Eingaben; Rückgabe: nur bekannte, geprüfte Felder."""
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Zeitplan: Objekt erwartet")
    out: dict = {}
    for key, value in data.items():
        if key == "auto_start":
            out[key] = bool(value)
        elif key in _SCHEDULE_SECTIONS:
            out[key] = _validate_schedule_section(key, value)
        else:
            raise HTTPException(status_code=400,
                                detail=f"Zeitplan: unbekanntes Feld '{key}'")
    return out


def schedule_defaults(section: str) -> dict:
    """Plausible Defaults für unvollständige Abschnitte (Scheduler-Fallbacks)."""
    return dict(_SCHEDULE_DEFAULTS.get(section) or {})


def update_schedule(instance_id: str, data: dict) -> dict:
    """Ändert den Zeitplan einer Instanz (merge: nur übergebene Felder)."""
    instance = _load_meta(instance_id)
    patch = _validate_schedule(data)
    schedule = dict(instance.get("schedule") or {})
    changed = []
    for key, value in patch.items():
        if isinstance(value, dict):
            merged = dict(schedule.get(key) or {})
            merged.update(value)
            schedule[key] = merged
        else:
            schedule[key] = value
        changed.append(f"schedule.{key}")
    instance["schedule"] = schedule
    _save_meta(instance)
    return {"instance": instance, "changed": changed}


def delete_instance(instance_id: str, force: bool = False) -> dict:
    """Löscht eine Instanz vollständig (Verzeichnis + Metadaten).

    force=True entfernt auch die Container-Reste; laufende Instanzen müssen
    zuerst gestoppt werden, es sei denn, force wird gesetzt.
    """
    from . import runtime  # lazy, vermeidet Import-Zirkel
    instance = _load_meta(instance_id)
    if runtime.is_running(instance):
        if not force:
            raise HTTPException(status_code=409,
                                detail="Instanz läuft noch — bitte zuerst stoppen "
                                       "(oder Löschen mit force bestätigen)")
        runtime.remove_container(instance)
    directory = instance_dir(instance_id)
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Löschen fehlgeschlagen: {exc}")
    return {"deleted": instance_id, "name": instance["name"]}


def mods_dir(instance_id: str):
    return instance_dir(instance_id) / "mods"


def clone_instance(source_id: str, name: str | None = None) -> dict:
    """Klont eine Instanz als Vorlage: kompletter Ordner (Welt, Mods,
    Configs) wird kopiert, Metadaten neu erzeugt (eigene ID, eigener Port).

    Nicht kopiert werden packs/ (Modpack-Archive sind nur Installations-
    Quellen) und instance.json. Der Klon startet gestoppt; laufende
    Quellen werden abgelehnt (inkonsistenter Welt-Snapshot).
    """
    from . import runtime  # lazy, vermeidet Import-Zirkel
    source = _load_meta(source_id)
    if runtime.is_running(source):
        raise HTTPException(status_code=409,
                            detail="Instanz läuft — bitte zuerst stoppen, "
                                   "damit die Welt konsistent kopiert wird")
    with _LOCK:
        existing = _scan()
        if name is None:
            new_name = unique_instance_name(source["name"])
        else:
            new_name = _validate_instance_name(name)
            if any(i["name"].lower() == new_name.lower() for i in existing):
                raise HTTPException(status_code=409,
                                    detail=f"Instanz-Name '{new_name}' ist bereits vergeben")
        used_game, used_rcon = _used_port_sets(existing)
        port = _validate_port(None, used_game, used_rcon)
        instance = {
            "id": uuid.uuid4().hex[:8],
            "name": new_name,
            "loader": source["loader"],
            "game_version": source["game_version"],
            "loader_version": source.get("loader_version"),
            "port": port,
            "rcon_port": port + 1000,
            "memory": source.get("memory"),
            "jvm_opts": source.get("jvm_opts"),
            "use_aikar": source.get("use_aikar", False),
            "schedule": source.get("schedule"),
            "tags": list(source.get("tags") or []),
            "created_at": int(time.time()),
            "status": "stopped",
            "error": None,
            "modpack": source.get("modpack"),
        }
        src_dir = instance_dir(source_id)
        dst_dir = instance_dir(instance["id"])
        try:
            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True, ignore=_CLONE_SKIP)
            (dst_dir / "packs").mkdir(exist_ok=True)
        except OSError as exc:
            shutil.rmtree(dst_dir, ignore_errors=True)
            raise HTTPException(status_code=500,
                                detail=f"Klonen fehlgeschlagen: {exc}")
        try:
            _save_meta(instance)
        except HTTPException:
            shutil.rmtree(dst_dir, ignore_errors=True)
            raise
    return instance


def find_world_dir(directory: Path) -> Path | None:
    """Findet den Welt-Ordner eines Server-Verzeichnisses (level.dat).
    Reihenfolge: level-name aus server.properties → 'world' → erster
    Unterordner mit level.dat."""
    level_name = "world"
    props = directory / "server.properties"
    if props.is_file():
        try:
            for line in props.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("level-name="):
                    level_name = line.split("=", 1)[1].strip() or "world"
                    break
        except OSError:
            pass
    for name in dict.fromkeys((level_name, "world")):
        if "/" in name or "\\" in name or name in (".", "..", ""):
            continue
        candidate = directory / name
        if candidate.is_dir() and (candidate / "level.dat").is_file():
            return candidate
    try:
        for entry in sorted(directory.iterdir()):
            if entry.is_dir() and not entry.is_symlink() \
                    and (entry / "level.dat").is_file():
                return entry
    except OSError:
        pass
    return None


def world_dir(instance_id: str) -> Path | None:
    return find_world_dir(instance_dir(instance_id))


def _dir_size(path: Path) -> int:
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            # scandir statt os.walk + Path.stat: ein Systemaufruf pro Eintrag
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


# Kurzer Cache für disk_usage: Welten mit vielen Dateien sind teuer zu
# vermessen; das Öffnen/Neuladen des Workspace darf nicht jedes Mal den
# kompletten Instanz-Ordner durchwandern.
_DISK_CACHE: dict = {}
_DISK_CACHE_TTL = 30.0


def invalidate_disk_cache(instance_id: str | None = None) -> None:
    """Vergessenen Speicher-Cache ungültig machen (nach Uploads/Installation)."""
    if instance_id is None:
        _DISK_CACHE.clear()
    else:
        _DISK_CACHE.pop(instance_id, None)


def disk_usage(instance_id: str) -> dict:
    """Speicherverbrauch der Instanz, aufgeschlüsselt nach Mods, Welt,
    packs und Rest (Welt = gefundener level.dat-Ordner). Ergebnis wird
    kurz (30 s) gecacht, damit häufiges Öffnen/Neuladen schnell bleibt."""
    now = time.monotonic()
    cached = _DISK_CACHE.get(instance_id)
    if cached is not None and now - cached[0] < _DISK_CACHE_TTL:
        return cached[1]
    directory = instance_dir(instance_id)
    world_path = find_world_dir(directory)
    world = _dir_size(world_path) if world_path else 0
    mods = _dir_size(directory / "mods")
    packs = _dir_size(directory / "packs")
    total = _dir_size(directory)
    result = {
        "total_bytes": total,
        "mods_bytes": mods,
        "world_bytes": world,
        "packs_bytes": packs,
        "rest_bytes": max(0, total - mods - world - packs),
        "world_dir": world_path.name if world_path else None,
        "world_exists": world_path is not None,
    }
    _DISK_CACHE[instance_id] = (now, result)
    return result


def list_mods(instance_id: str) -> list:
    """Mods einer einzelnen Instanz (getrennt von anderen Instanzen)."""
    return list_mods_in(mods_dir(instance_id))


def list_mods_in(directory) -> list:
    """Mods in einem mods-Ordner; deaktivierte (*.jar.disabled) inklusive."""
    mods = []
    try:
        for path in directory.glob("*.jar*"):
            # Nur aktive .jar und deaktivierte .jar.disabled berücksichtigen
            if path.suffix not in (".jar", ".disabled"):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            mods.append({
                "filename": path.name,
                "enabled": path.suffix == ".jar",
                "size_bytes": stat.st_size,
                "modified": int(stat.st_mtime),
            })
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Mods-Ordner nicht lesbar: {exc}")
    mods.sort(key=lambda m: m["filename"].lower())
    return mods


def toggle_mod(instance_id: str, filename: str, enabled: bool) -> dict:
    """Aktiviert/deaktiviert eine Instanz-Mod per Umbenennen (.jar <-> .jar.disabled)."""
    return toggle_mod_in(mods_dir(instance_id), filename, enabled)


def toggle_mod_in(directory, filename: str, enabled: bool) -> dict:
    """Aktiviert/deaktiviert eine Mod in einem mods-Ordner per Umbenennen
    (.jar <-> .jar.disabled); deaktivierte Dateien ignoriert der Server."""
    from .security import safe_mods_path
    src = safe_mods_path(directory, filename)
    if enabled and filename.endswith(".disabled"):
        target_name = filename[: -len(".disabled")]
    elif not enabled and not filename.endswith(".disabled"):
        target_name = filename + ".disabled"
    else:
        return {"filename": filename, "enabled": enabled}  # bereits im Zielzustand
    dest = safe_mods_path(directory, target_name)
    if not src.is_file():
        raise HTTPException(status_code=404, detail="Mod nicht gefunden")
    if dest.exists():
        raise HTTPException(status_code=409,
                            detail=f"Zieldatei '{target_name}' existiert bereits")
    try:
        os.replace(src, dest)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Umbenennen fehlgeschlagen: {exc}")
    return {"filename": target_name, "enabled": enabled}


def delete_mod(instance_id: str, filename: str) -> str:
    from .security import safe_mods_path
    path = safe_mods_path(mods_dir(instance_id), filename)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Mod in dieser Instanz nicht gefunden")
    try:
        path.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Löschen fehlgeschlagen: {exc}")
    return filename


def pack_dir(instance_id: str):
    return instance_dir(instance_id) / "packs"


def validate_pack_filename(filename: str) -> str:
    """Modpack-Archive (.mrpack Modrinth, .zip CurseForge) mit Namensschema prüfen."""
    if not filename or len(filename) > 255:
        raise HTTPException(status_code=400, detail="Ungültiger Modpack-Dateiname")
    if "/" in filename or "\\" in filename or ".." in filename or "\x00" in filename:
        raise HTTPException(status_code=400, detail="Ungültiger Modpack-Dateiname")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 _.\-()\[\]]*\.(mrpack|zip)$", filename):
        raise HTTPException(status_code=400, detail="Nur .mrpack- oder .zip-Dateien erlaubt")
    return filename


# ---------------------------------------------------------------------------
# server.properties lesen/schreiben (Einstellungen im Detail-Dialog)
# ---------------------------------------------------------------------------

_PROP_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# ---------------------------------------------------------------------------
# Bekannte server.properties-Einträge (Formular-Editor im Frontend).
# Typen: bool, int, enum, str; managed = wird vom Dashboard verwaltet
# und im Formular nur angezeigt. Unbekannte Keys bleiben raw editierbar.
# ---------------------------------------------------------------------------

_PROPS_SCHEMA: list[dict] = [
    {"key": "motd", "label": "Beschreibung (MOTD)", "type": "str",
     "hint": "Nachricht in der Serverliste"},
    {"key": "max-players", "label": "Maximale Spieler", "type": "int",
     "min": 0, "max": 2147483647},
    {"key": "online-mode", "label": "Online-Mode (echte Accounts)", "type": "bool",
     "hint": "false = Offline/Cracked — nur bewusst und in eigenem Netz"},
    {"key": "difficulty", "label": "Schwierigkeit", "type": "enum",
     "choices": ["peaceful", "easy", "normal", "hard"]},
    {"key": "gamemode", "label": "Standard-Spielmodus", "type": "enum",
     "choices": ["survival", "creative", "adventure", "spectator"]},
    {"key": "force-gamemode", "label": "Spielmodus beim Join erzwingen", "type": "bool"},
    {"key": "hardcore", "label": "Hardcore-Modus", "type": "bool"},
    {"key": "pvp", "label": "PvP erlauben", "type": "bool"},
    {"key": "allow-flight", "label": "Fliegen erlauben", "type": "bool",
     "hint": "auch für Mods/Flug-Overrides"},
    {"key": "allow-nether", "label": "Nether erlauben", "type": "bool"},
    {"key": "view-distance", "label": "View-Distance (Chunks)", "type": "int",
     "min": 3, "max": 32, "hint": "Haupt-Hebel für RAM/Performance"},
    {"key": "simulation-distance", "label": "Simulation-Distance (Chunks)",
     "type": "int", "min": 3, "max": 32},
    {"key": "spawn-protection", "label": "Spawn-Schutz (Radius)", "type": "int",
     "min": 0, "max": 256},
    {"key": "white-list", "label": "Whitelist aktiv", "type": "bool"},
    {"key": "enforce-whitelist", "label": "Whitelist erzwingen", "type": "bool",
     "hint": "gilt auch für OPs"},
    {"key": "spawn-animals", "label": "Tiere spawnen", "type": "bool"},
    {"key": "spawn-monsters", "label": "Monster spawnen", "type": "bool"},
    {"key": "spawn-npcs", "label": "Dorfbewohner/NPCs spawnen", "type": "bool"},
    {"key": "spawn-villagers", "label": "Dorfbewohner-Vermehrung", "type": "bool"},
    {"key": "op-permission-level", "label": "OP-Permission-Level", "type": "int",
     "min": 0, "max": 4},
    {"key": "function-permission-level", "label": "Funktionen-Permission-Level",
     "type": "int", "min": 0, "max": 4},
    {"key": "player-idle-timeout", "label": "Idle-Kick (Minuten, 0 = aus)",
     "type": "int", "min": 0, "max": 35000},
    {"key": "max-tick-time", "label": "Max-Tick-Time (ms, 0 = aus)", "type": "int",
     "min": 0, "max": 2147483647,
     "hint": "Server stoppt bei Überschreitung (Watchdog)"},
    {"key": "max-world-size", "label": "Max. Weltgröße (Blöcke Radius)", "type": "int",
     "min": 1, "max": 29999984},
    {"key": "entity-broadcast-range-percentage", "label": "Entity-Broadcast-Range (%)",
     "type": "int", "min": 0, "max": 500,
     "hint": "kleiner = weniger Traffic"},
    {"key": "network-compression-threshold", "label": "Netz-Kompression (Bytes, -1 = aus)",
     "type": "int", "min": -1, "max": 2147483647},
    {"key": "spawn-radius", "label": "Spawn-Radius", "type": "int", "min": 0,
     "max": 2147483647},
    {"key": "rate-limit", "label": "Packet-Rate-Limit (B/s, 0 = aus)", "type": "int",
     "min": 0, "max": 2147483647},
    {"key": "pause-when-empty-seconds", "label": "Pause ohne Spieler (Sekunden)",
     "type": "int", "min": 0, "max": 86400,
     "hint": "ab 1.21.2 — Welt-Tick pausieren"},
    {"key": "require-resource-pack", "label": "Ressourcenpaket erzwingen", "type": "bool"},
    {"key": "resource-pack", "label": "Ressourcenpaket (URL)", "type": "str"},
    {"key": "resource-pack-sha1", "label": "Ressourcenpaket (SHA1)", "type": "str"},
    {"key": "resource-pack-prompt", "label": "Ressourcenpaket-Hinweis", "type": "str"},
    {"key": "level-seed", "label": "Welt-Seed", "type": "str",
     "hint": "wirkt nur bei neu erzeugter Welt"},
    {"key": "level-type", "label": "Welt-Typ", "type": "str",
     "hint": "z. B. minecraft:normal, minecraft:flat, minecraft:amplified"},
    {"key": "enable-command-block", "label": "Command-Blöcke aktiv", "type": "bool"},
    {"key": "enable-status", "label": "Serverliste-Antwort aktiv", "type": "bool"},
    {"key": "hide-online-players", "label": "Spielerzahl verbergen", "type": "bool"},
    {"key": "enforce-secure-profile", "label": "Signierte Profile erzwingen",
     "type": "bool", "hint": "off = Chat-Signaturen deaktiviert"},
    {"key": "sync-chunk-writes", "label": "Chunk-Writes synchron", "type": "bool"},
    {"key": "prevent-proxy-connections", "label": "Proxy-Verbindungen blockieren",
     "type": "bool"},
    {"key": "use-native-transport", "label": "Native Netz-Transport", "type": "bool"},
    {"key": "accept-transfers", "label": "Server-Transfers akzeptieren", "type": "bool"},
    {"key": "broadcast-console-to-ops", "label": "Konsole an OPs senden", "type": "bool"},
    {"key": "bug-report-link", "label": "Bug-Report-Link", "type": "str"},
    {"key": "initial-enabled-packs", "label": "Datapacks initial aktiv", "type": "str"},
    {"key": "initial-disabled-packs", "label": "Datapacks initial deaktiviert",
     "type": "str"},
    # Vom Dashboard verwaltet — im Formular nur lesend:
    {"key": "server-port", "label": "Server-Port (Container-intern)", "managed": True,
     "hint": "immer 25565 im Container; den Host-Port ändert man im Detail-Dialog"},
    {"key": "level-name", "label": "Welt-Ordner", "managed": True,
     "hint": "wird vom Dashboard (Welt-Import/Restore) gesetzt"},
    {"key": "enable-rcon", "label": "RCON aktiv", "managed": True,
     "hint": "vom Dashboard gesetzt (Spieler-Verwaltung, Konsole)"},
    {"key": "rcon.port", "label": "RCON-Port (Container-intern)", "managed": True,
     "hint": "immer 25575 im Container; Host-Port = Instanz-Port + 1000"},
    {"key": "rcon.password", "label": "RCON-Passwort", "managed": True,
     "hint": "vom Dashboard gesetzt"},
    {"key": "query.port", "label": "Query-Port", "managed": True},
]

_PROP_SCHEMA_BY_KEY: dict[str, dict] = {entry["key"]: entry for entry in _PROPS_SCHEMA}


def properties_schema() -> list[dict]:
    """Schema bekannter server.properties-Einträge für den Formular-Editor."""
    return [dict(entry) for entry in _PROPS_SCHEMA]


def _validate_property_value(key: str, value: str) -> str:
    """Validiert einen bekannten server.properties-Wert (bool/int/enum);
    unbekannte und verwaltete Keys bleiben unverändert."""
    rule = _PROP_SCHEMA_BY_KEY.get(key)
    if not rule or rule.get("managed"):
        return value
    raw = value.strip()
    kind = rule.get("type")
    if kind == "bool":
        low = raw.lower()
        if low not in ("true", "false"):
            raise HTTPException(status_code=400,
                                detail=f"'{key}' muss 'true' oder 'false' sein")
        return low
    if kind == "enum":
        low = raw.lower()
        choices = rule.get("choices") or []
        if low not in choices:
            raise HTTPException(
                status_code=400,
                detail=f"'{key}' muss einer von {', '.join(choices)} sein")
        return low
    if kind == "int":
        try:
            number = int(raw)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=f"'{key}' muss eine ganze Zahl sein")
        lo, hi = rule.get("min"), rule.get("max")
        if (lo is not None and number < lo) or (hi is not None and number > hi):
            raise HTTPException(
                status_code=400,
                detail=f"'{key}' muss zwischen {lo} und {hi} liegen")
        return str(number)
    return value


def read_server_properties(instance_id: str) -> list:
    """server.properties als geordnete Liste {key, value} (Kommentare übersprungen)."""
    path = instance_dir(instance_id) / "server.properties"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="server.properties existiert noch nicht — Instanz einmal starten")
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f"server.properties nicht lesbar: {exc}")
    props = []
    seen = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        props.append({"key": key, "value": value})
    return props


def write_server_properties(instance_id: str, properties: list,
                            managed_ok: bool = False) -> int:
    """Schreibt server.properties atomar; validiert Schlüssel und Werte
    (bekannte Keys per Schema: bool/int/enum). Verwaltete Keys (server-port,
    level-name, rcon.*) werden außer bei internen Aufrufern (managed_ok=True,
    z. B. Welt-Import/Restore) auf den bisherigen Dateistand gezwungen —
    fehlen sie in der Datei, werden sie nicht neu angelegt. Duplikate werden
    ignoriert (erster Eintrag gewinnt)."""
    current: dict[str, str] = {}
    if not managed_ok:
        try:
            for line in (instance_dir(instance_id) / "server.properties") \
                    .read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                existing_key, existing_value = stripped.split("=", 1)
                current[existing_key] = existing_value
        except (FileNotFoundError, OSError):
            current = {}
    cleaned = []
    seen = set()
    for item in properties:
        key = str(item.get("key") or "").strip()
        value = str(item.get("value") or "")
        if not _PROP_KEY_RE.match(key):
            raise HTTPException(status_code=400,
                                detail=f"Ungültiger Eigenschaftsname: '{key[:64]}'")
        if "\r" in value or "\n" in value or "\x00" in value:
            raise HTTPException(status_code=400,
                                detail=f"Wert von '{key}' darf keine Zeilenumbrüche enthalten")
        if len(value) > 512:
            raise HTTPException(status_code=400, detail=f"Wert von '{key}' ist zu lang (max. 512)")
        rule = _PROP_SCHEMA_BY_KEY.get(key)
        if not managed_ok and rule and rule.get("managed"):
            if key not in current:
                continue  # nicht vom Dashboard gesetzt → nicht neu anlegen
            value = current[key]  # verwaltet: Dateistand bewahren
        value = _validate_property_value(key, value)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append((key, value))
    path = instance_dir(instance_id) / "server.properties"
    tmp = path.with_name("server.properties.tmp")
    try:
        tmp.write_text(
            "# Erstellt/bearbeitet über das Minecraft-Dashboard\n"
            + "".join(f"{key}={value}\n" for key, value in cleaned),
            encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=500,
                            detail=f"server.properties nicht schreibbar: {exc}")
    return len(cleaned)


__all__ = [
    "clone_instance",
    "create_instance",
    "delete_instance",
    "delete_mod",
    "disk_usage",
    "find_world_dir",
    "get_instance",
    "instance_dir",
    "list_instances",
    "list_mods",
    "list_mods_in",
    "mods_dir",
    "pack_dir",
    "properties_schema",
    "read_server_properties",
    "schedule_defaults",
    "set_status",
    "toggle_mod",
    "toggle_mod_in",
    "unique_instance_name",
    "update_instance",
    "update_schedule",
    "update_settings",
    "validate_pack_filename",
    "world_dir",
    "write_server_properties",
]
