"""Welt-Verwaltung + Server-Import: Zip-Download des Welt-Ordners,
Welt-Upload (.zip/.tar.gz) und Import bestehender Server-Ordner als Instanz.

Sicherheit: Archiv-Einträge werden strikt validiert (keine absoluten Pfade,
kein '..', ':' im Pfad), Gesamtgröße und Dateianzahl sind gedeckelt; tar.gz
wird mit PEP-706-Filter 'data' extrahiert (neutralisiert Traversal/Symlinks).
"""
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

from fastapi import HTTPException

from . import instances
from .config import settings

# Entpackte Gesamtgröße: Welten/ganze Server-Ordner können groß sein, aber
# ohne Deckel wäre ein Zip-Bomben-Angriff möglich (komprimiert nur wenige MiB)
_MAX_EXTRACT_BYTES = 8 * 1024 ** 3
_MAX_MEMBERS = 50_000
_CHUNK = 1024 * 1024

_ARCHIVE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-()\[\]]*\.(zip|tar\.gz)$")
_ZIP_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")

# Welt-Dateien/-Ordner (Vanilla+Nether/End), wenn der Welt-Inhalt direkt im
# Server-Ordner liegt (level.dat auf oberster Ebene) und nach world/ umziehen muss
_WORLD_ITEMS = (
    "level.dat", "level.dat_old", "session.lock", "uid.dat", "icon.png",
    "region", "playerdata", "DIM-1", "DIM1", "datapacks", "stats",
    "advancements",
)


def validate_archive_filename(filename: str) -> str:
    """Server-/Welt-Archive (.zip oder .tar.gz) mit Namensschema prüfen."""
    if not filename or len(filename) > 255:
        raise HTTPException(status_code=400, detail="Ungültiger Archiv-Dateiname")
    if "/" in filename or "\\" in filename or ".." in filename or "\x00" in filename:
        raise HTTPException(status_code=400, detail="Ungültiger Archiv-Dateiname")
    if not _ARCHIVE_RE.match(filename):
        raise HTTPException(status_code=400,
                            detail="Nur .zip- oder .tar.gz-Dateien erlaubt")
    return filename


def staging_dir() -> Path:
    """Zwischenablage im Instanz-Volume (gleiches Dateisystem für Umbenennen)."""
    path = settings.instances_dir / "_staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Extraktion (zip + tar.gz) mit Namens-/Größen-Deckelung
# ---------------------------------------------------------------------------

def _iter_zip(zf: zipfile.ZipFile):
    """Validierte (name, info)-Paare eines Zip-Archivs (Traversal-sicher)."""
    if len(zf.infolist()) > _MAX_MEMBERS:
        raise HTTPException(status_code=400,
                            detail=f"Archiv enthält zu viele Dateien (max. {_MAX_MEMBERS})")
    total = 0
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/") or ":" in name:
            raise HTTPException(status_code=400,
                                detail=f"Unerlaubter Pfad im Archiv: {info.filename!r}")
        if info.is_dir():
            continue
        total += int(info.file_size or 0)
        if total > _MAX_EXTRACT_BYTES:
            raise HTTPException(status_code=413,
                                detail="Archiv-Inhalt zu groß (max. 8 GiB entpackt)")
        yield name, info


def _extract_zip(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    resolved = dest.resolve()
    with zipfile.ZipFile(src) as zf:
        for name, info in _iter_zip(zf):
            target = (dest / name).resolve()
            if not target.is_relative_to(resolved):
                raise HTTPException(status_code=400,
                                    detail=f"Pfadmanipulation im Archiv: {name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src_fh, open(target, "wb") as out:
                shutil.copyfileobj(src_fh, out, _CHUNK)


def _extract_tar(src: Path, dest: Path) -> None:
    import tarfile
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(src, "r:gz") as tf:
        members = tf.getmembers()
        if len(members) > _MAX_MEMBERS:
            raise HTTPException(status_code=400,
                                detail=f"Archiv enthält zu viele Dateien (max. {_MAX_MEMBERS})")
        if sum(m.size for m in members) > _MAX_EXTRACT_BYTES:
            raise HTTPException(status_code=413,
                                detail="Archiv-Inhalt zu groß (max. 8 GiB entpackt)")
        # Filter 'data' (PEP 706): neutralisiert Pfad-Traversal und Symlinks
        tf.extractall(path=dest, filter="data")


def _extract_archive(src: Path, dest: Path, original_name: str) -> None:
    try:
        if original_name.lower().endswith(".tar.gz"):
            _extract_tar(src, dest)
        else:
            _extract_zip(src, dest)
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise HTTPException(status_code=400,
                            detail=f"Archiv nicht entpackbar: {exc}") from exc


def _detect_world(staging: Path):
    """(world_name, world_path) im entpackten Ordner: level.dat am Wurzel-
    verzeichnis (Welt = ganzer Ordner) oder erster Unterordner mit level.dat."""
    if (staging / "level.dat").is_file():
        return None, staging
    try:
        for entry in sorted(staging.iterdir()):
            if entry.is_dir() and not entry.is_symlink() \
                    and (entry / "level.dat").is_file():
                return entry.name, entry
    except OSError:
        pass
    return None, None


def _patch_properties(instance_id: str, updates: dict) -> None:
    """server.properties ergänzen/überschreiben (erzeugt die Datei bei Bedarf)."""
    try:
        props = instances.read_server_properties(instance_id)
    except HTTPException:
        props = []  # Datei existiert noch nicht (frisch importierte Instanz)
    values = {p["key"]: p["value"] for p in props}
    values.update(updates)
    instances.write_server_properties(
        instance_id, [{"key": key, "value": value} for key, value in values.items()],
        managed_ok=True)  # level-name/server-port werden vom Import verwaltet


def _clear_dir(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _move_contents(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dest / child.name
        if target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        shutil.move(str(child), str(target))


# ---------------------------------------------------------------------------
# Welt: Info + Zip-Download + Upload
# ---------------------------------------------------------------------------

def world_info(instance_id: str) -> dict:
    usage = instances.disk_usage(instance_id)
    return {"world_dir": usage["world_dir"], "exists": usage["world_exists"],
            "size_bytes": usage["world_bytes"]}


def create_world_zip(instance_id: str) -> dict:
    """Packt den Welt-Ordner als .zip in die Staging-Ablage (Download)."""
    world = instances.world_dir(instance_id)
    if world is None:
        raise HTTPException(status_code=404,
                            detail="Keine Welt gefunden — Server einmal starten "
                                   "(oder Welt hochladen)")
    safe = _ZIP_NAME_RE.sub("_", world.name) or "world"
    dest = staging_dir() / f"{safe}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.zip"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, _dirs, files in os.walk(world, onerror=None):
            for file in files:
                path = Path(root) / file
                try:
                    zf.write(path, arcname=str(path.relative_to(world)))
                except OSError:
                    continue
    return {"name": dest.name, "path": dest, "world": world.name,
            "size_bytes": dest.stat().st_size}


def restore_world_upload(instance_id: str, src: Path, original_name: str) -> dict:
    """Ersetzt die Welt der (gestoppten) Instanz durch das hochgeladene Archiv.

    Zwei Layouts: Welt-Ordner als Wurzel (level.dat am Anfang) oder Welt als
    einzelner Unterordner — der Unterordner-Name wird übernommen und als
    level-name in server.properties gesetzt.
    """
    from . import runtime  # lazy, vermeidet Import-Zirkel
    instance = instances.get_instance(instance_id)
    if runtime.is_running(instance):
        raise HTTPException(status_code=409,
                            detail="Instanz läuft — Welt-Upload nur bei gestoppter Instanz")
    directory = instances.instance_dir(instance_id)
    staging = Path(tempfile.mkdtemp(prefix="world_", dir=staging_dir()))
    try:
        _extract_archive(src, staging, original_name)
        world_name, world_path = _detect_world(staging)
        if world_path is None:
            raise HTTPException(status_code=400,
                                detail="Kein Welt-Ordner gefunden (level.dat fehlt) — "
                                       "die .zip muss die Welt oder deren Inhalt enthalten")
        if world_name is None:
            # Welt-Inhalt direkt im Archiv-Wurzelverzeichnis: in den
            # bestehenden Welt-Ordner (level-name bzw. 'world') entpacken
            target = instances.find_world_dir(directory) or (directory / "world")
        else:
            # Alten Welt-Ordner (abweichender Name) entfernen, damit keine
            # verwaiste Doppel-Welt im Instanz-Ordner bleibt
            previous = instances.find_world_dir(directory)
            if previous is not None and previous.name != world_name:
                shutil.rmtree(previous, ignore_errors=True)
            target = directory / world_name
        _clear_dir(target)
        _move_contents(world_path, target)
        # level-name immer auf den Ziel-Ordner setzen, damit die hochgeladene
        # Welt garantiert verwendet wird (auch wenn Properties abweichen)
        _patch_properties(instance_id, {"level-name": target.name})
        return {"world_dir": target.name, "restored": original_name}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# Server-Import: bestehenden Server-Ordner als neue Instanz einbinden
# ---------------------------------------------------------------------------

def import_server(src: Path, original_name: str, *, name: str, loader: str,
                  game_version: str, loader_version: str = None,
                  port: int = None, memory: str = None,
                  accept_eula: bool = False) -> dict:
    """Erstellt eine neue Instanz aus einem hochgeladenen Server-Archiv
    (.zip/.tar.gz mit Welt/Mods/Configs).

    Die Archiv-Dateien landen im Instanz-Ordner; server.properties wird auf
    die Container-Erfordernisse angepasst (Port 25565 im Container, RCON),
    eula.txt erzwungen akzeptiert und eine gefundene Welt verdrahtet.
    Bei Fehlern wird die angelegte Instanz vollständig entfernt.
    """
    instance = instances.create_instance(
        name, loader, game_version, loader_version=loader_version, port=port,
        memory=memory, accept_eula=accept_eula)
    directory = instances.instance_dir(instance["id"])
    staging = Path(tempfile.mkdtemp(prefix="import_", dir=staging_dir()))
    try:
        _extract_archive(src, staging, original_name)
        _move_contents(staging, directory)
        # Archiv könnte versehentlich ein instance.json mitbringen — unsere
        # frische Metadaten erzwingen (unser ID/Port/Status gewinnt)
        instances.update_instance(instance)
        world_name, world_path = _detect_world(directory)
        if world_name is None and world_path is not None:
            # Welt-Inhalt liegt direkt im Server-Ordner → nach world/ umziehen
            world_target = directory / "world"
            world_target.mkdir(exist_ok=True)
            for item in _WORLD_ITEMS:
                child = directory / item
                if child.exists():
                    target = world_target / item
                    if target.exists():
                        if target.is_dir() and not target.is_symlink():
                            shutil.rmtree(target, ignore_errors=True)
                        else:
                            target.unlink(missing_ok=True)
                    shutil.move(str(child), str(target))
            world_name = "world"
        updates = {"server-port": "25565", "enable-rcon": "true",
                   "rcon.port": "25575"}
        if world_name:
            updates["level-name"] = world_name
        _patch_properties(instance["id"], updates)
        # EULA der Instanz erzwingen (Archiv könnte eula=false mitbringen)
        (directory / "eula.txt").write_text("eula=true\n", encoding="utf-8")
        return instance
    except HTTPException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    except (OSError, ValueError) as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(status_code=500,
                            detail=f"Import fehlgeschlagen: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# Mehrere Welten je Instanz: auflisten, neu anlegen, wechseln, kopieren,
# umbenennen, löschen, als zusätzliche Welt importieren.
#
# Eine Welt ist ein Unterordner des Instanz-Ordners mit level.dat. Aktiv ist
# die Welt aus level-name (server.properties). Paper/Spigot/Bukkit legen
# Nether/End als Geschwister-Ordner '<welt>_nether' / '<welt>_the_end' an —
# die gehören zur Welt und werden bei allen Operationen mitgenommen.
# ---------------------------------------------------------------------------

_WORLD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,47}$")
_COMPANION_SUFFIXES = ("_nether", "_the_end")
# Ordner, die nie als Welt-Name taugen (Server-Struktur)
_RESERVED_DIRS = frozenset({
    "mods", "config", "plugins", "logs", "crash-reports", "libraries",
    "packs", "versions", "defaultconfigs", "kubejs", "bluemap", "backups",
    "datapacks", "resourcepacks", "world-backups", "cache", ".fabric",
    "_staging",
})
_LEVEL_TYPES = ("normal", "flat", "large_biomes", "amplified")
# Vor 1.19 hießen die Welt-Typen ohne Namespace (largeBiomes in CamelCase)
_LEGACY_LEVEL_TYPES = {"normal": "default", "flat": "flat",
                       "large_biomes": "largeBiomes", "amplified": "amplified"}


def validate_world_name(name: str) -> str:
    name = (name or "").strip()
    if not _WORLD_NAME_RE.match(name) or ".." in name:
        raise HTTPException(
            status_code=400,
            detail="Ungültiger Welt-Name — 1-48 Zeichen: Buchstaben, Ziffern, "
                   "Leerzeichen, _ . -")
    lower = name.lower()
    if lower in _RESERVED_DIRS or lower.endswith(_COMPANION_SUFFIXES):
        raise HTTPException(status_code=400,
                            detail=f"„{name}“ ist als Welt-Name reserviert")
    return name


def _active_level_name(directory: Path) -> str:
    props = directory / "server.properties"
    try:
        for line in props.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("level-name="):
                return line.split("=", 1)[1].strip() or "world"
    except OSError:
        pass
    return "world"


def _world_parts(directory: Path, name: str) -> list[Path]:
    """Welt-Ordner plus vorhandene Paper-Geschwister (Nether/End)."""
    parts = [directory / name]
    for suffix in _COMPANION_SUFFIXES:
        sibling = directory / f"{name}{suffix}"
        if sibling.is_dir() and not sibling.is_symlink():
            parts.append(sibling)
    return parts


def _is_world(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and (path / "level.dat").is_file()


def _game_minor(game_version: str) -> int:
    match = re.match(r"^1\.(\d+)", str(game_version or ""))
    return int(match.group(1)) if match else 99  # Snapshots/unbekannt = neu


def _level_type_value(level_type: str, game_version: str) -> str:
    if level_type not in _LEVEL_TYPES:
        raise HTTPException(status_code=400,
                            detail=f"Welt-Typ muss einer von {', '.join(_LEVEL_TYPES)} sein")
    if _game_minor(game_version) >= 19:
        return f"minecraft:{level_type}"
    return _LEGACY_LEVEL_TYPES[level_type]


def _ensure_stopped(instance: dict, what: str) -> None:
    from . import runtime  # lazy, vermeidet Import-Zirkel
    if runtime.is_running(instance):
        raise HTTPException(status_code=409,
                            detail=f"Instanz läuft — {what} nur bei gestopptem Server")


def list_worlds(instance_id: str) -> dict:
    """Alle Welten der Instanz: Name, Größe, letzte Änderung, aktiv-Flag.
    Ist die aktive Welt noch nicht erzeugt (neu angelegt, Server nie
    gestartet), erscheint sie mit pending=True."""
    directory = instances.instance_dir(instance_id)
    active = _active_level_name(directory)
    names = []
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        entries = []
    all_names = {e.name for e in entries}
    for entry in entries:
        if not _is_world(entry):
            continue
        base = next((entry.name[: -len(s)] for s in _COMPANION_SUFFIXES
                     if entry.name.endswith(s)), None)
        if base and base in all_names:
            continue  # Paper-Nether/End gehören zur Basis-Welt
        names.append(entry.name)
    worlds_out = []
    for name in names:
        parts = _world_parts(directory, name)
        try:
            modified = int((directory / name / "level.dat").stat().st_mtime)
        except OSError:
            modified = None
        worlds_out.append({
            "name": name,
            "active": name == active,
            "pending": False,
            "size_bytes": sum(instances._dir_size(p) for p in parts),
            "modified": modified,
            "dimensions": [p.name for p in parts[1:]],
        })
    if active not in names:
        worlds_out.insert(0, {"name": active, "active": True, "pending": True,
                              "size_bytes": 0, "modified": None, "dimensions": []})
    worlds_out.sort(key=lambda w: (not w["active"], w["name"].lower()))
    return {"active": active, "worlds": worlds_out}


def _world_exists(directory: Path, name: str) -> bool:
    return (directory / name).exists()


def create_world(instance_id: str, name: str, seed: str = "",
                 level_type: str = "normal") -> dict:
    """Legt eine neue Welt an: setzt level-name/-seed/-type, die Welt wird
    beim nächsten Start erzeugt. Die bisherige Welt bleibt erhalten."""
    instance = instances.get_instance(instance_id)
    _ensure_stopped(instance, "eine neue Welt anlegen")
    name = validate_world_name(name)
    seed = (seed or "").strip()
    if len(seed) > 64 or any(ord(c) < 32 for c in seed):
        raise HTTPException(status_code=400, detail="Ungültiger Seed (max. 64 Zeichen)")
    directory = instances.instance_dir(instance_id)
    if _world_exists(directory, name):
        raise HTTPException(status_code=409, detail=f"„{name}“ existiert bereits")
    _patch_properties(instance_id, {
        "level-name": name,
        "level-seed": seed,
        "level-type": _level_type_value(level_type, str(instance.get("game_version") or "")),
    })
    instances.invalidate_disk_cache(instance_id)
    return {"active": name, "pending": True}


def switch_world(instance_id: str, name: str) -> dict:
    """Aktiviert eine vorhandene Welt (wirksam beim nächsten Start)."""
    instance = instances.get_instance(instance_id)
    _ensure_stopped(instance, "der Welt-Wechsel")
    directory = instances.instance_dir(instance_id)
    name = validate_world_name(name)
    if not _is_world(directory / name):
        raise HTTPException(status_code=404, detail=f"Welt „{name}“ nicht gefunden")
    _patch_properties(instance_id, {"level-name": name})
    instances.invalidate_disk_cache(instance_id)
    return {"active": name}


def _check_target_free(directory: Path, new: str) -> None:
    for path in _world_parts_names(new):
        if (directory / path).exists():
            raise HTTPException(status_code=409, detail=f"„{path}“ existiert bereits")


def _world_parts_names(name: str) -> list[str]:
    return [name] + [f"{name}{s}" for s in _COMPANION_SUFFIXES]


def copy_world(instance_id: str, name: str, new_name: str) -> dict:
    """Kopiert eine Welt (inkl. Nether/End-Geschwister) unter neuem Namen."""
    instance = instances.get_instance(instance_id)
    directory = instances.instance_dir(instance_id)
    name = validate_world_name(name)
    new_name = validate_world_name(new_name)
    if not _is_world(directory / name):
        raise HTTPException(status_code=404, detail=f"Welt „{name}“ nicht gefunden")
    if name == _active_level_name(directory):
        # Kopie einer laufenden Welt wäre inkonsistent (Region-Dateien offen)
        _ensure_stopped(instance, "das Kopieren der aktiven Welt")
    _check_target_free(directory, new_name)
    created = []
    try:
        for part in _world_parts(directory, name):
            target = directory / (new_name + part.name[len(name):])
            shutil.copytree(part, target, ignore=shutil.ignore_patterns("session.lock"))
            created.append(target)
            # Paper erkennt Welten an uid.dat — die Kopie bekommt eine neue
            (target / "uid.dat").unlink(missing_ok=True)
    except OSError as exc:
        for path in created:
            shutil.rmtree(path, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Kopieren fehlgeschlagen: {exc}") from exc
    instances.invalidate_disk_cache(instance_id)
    return {"name": new_name}


def rename_world(instance_id: str, name: str, new_name: str) -> dict:
    instance = instances.get_instance(instance_id)
    directory = instances.instance_dir(instance_id)
    name = validate_world_name(name)
    new_name = validate_world_name(new_name)
    if not _is_world(directory / name):
        raise HTTPException(status_code=404, detail=f"Welt „{name}“ nicht gefunden")
    active = name == _active_level_name(directory)
    if active:
        _ensure_stopped(instance, "das Umbenennen der aktiven Welt")
    _check_target_free(directory, new_name)
    for part in _world_parts(directory, name):
        part.rename(directory / (new_name + part.name[len(name):]))
    if active:
        _patch_properties(instance_id, {"level-name": new_name})
    instances.invalidate_disk_cache(instance_id)
    return {"name": new_name, "active": active}


def delete_world(instance_id: str, name: str) -> dict:
    """Löscht eine nicht aktive Welt endgültig (inkl. Nether/End)."""
    instances.get_instance(instance_id)
    directory = instances.instance_dir(instance_id)
    name = validate_world_name(name)
    if name == _active_level_name(directory):
        raise HTTPException(status_code=409,
                            detail="Die aktive Welt kann nicht gelöscht werden — "
                                   "erst zu einer anderen Welt wechseln")
    if not _is_world(directory / name):
        raise HTTPException(status_code=404, detail=f"Welt „{name}“ nicht gefunden")
    for part in _world_parts(directory, name):
        shutil.rmtree(part, ignore_errors=True)
    instances.invalidate_disk_cache(instance_id)
    return {"deleted": name}


def import_world(instance_id: str, src: Path, original_name: str,
                 name: str | None = None, activate: bool = False) -> dict:
    """Fügt ein Welt-Archiv als ZUSÄTZLICHE Welt hinzu (die aktive Welt
    bleibt unangetastet). Name: Parameter, sonst Ordnername im Archiv bzw.
    Archivname."""
    instance = instances.get_instance(instance_id)
    if activate:
        _ensure_stopped(instance, "der Welt-Wechsel")
    directory = instances.instance_dir(instance_id)
    staging = Path(tempfile.mkdtemp(prefix="world_", dir=staging_dir()))
    try:
        _extract_archive(src, staging, original_name)
        found_name, world_path = _detect_world(staging)
        if world_path is None:
            raise HTTPException(status_code=400,
                                detail="Kein Welt-Ordner gefunden (level.dat fehlt)")
        if not name:
            stem = re.sub(r"\.(zip|tar\.gz)$", "", original_name, flags=re.I)
            name = found_name or stem
            name = re.sub(r"[^A-Za-z0-9 _.\-]", "_", name).strip(" ._-")[:48] or "welt"
        name = validate_world_name(name)
        _check_target_free(directory, name)
        _move_contents(world_path, directory / name)
        (directory / name / "session.lock").unlink(missing_ok=True)
        if activate:
            _patch_properties(instance_id, {"level-name": name})
        instances.invalidate_disk_cache(instance_id)
        return {"name": name, "active": activate}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def create_named_world_zip(instance_id: str, name: str) -> dict:
    """Wie create_world_zip, aber für eine bestimmte (nicht aktive) Welt."""
    directory = instances.instance_dir(instance_id)
    name = validate_world_name(name)
    world = directory / name
    if not _is_world(world):
        raise HTTPException(status_code=404, detail=f"Welt „{name}“ nicht gefunden")
    safe = _ZIP_NAME_RE.sub("_", name) or "world"
    dest = staging_dir() / f"{safe}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.zip"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, _dirs, files in os.walk(world):
            for file in files:
                path = Path(root) / file
                try:
                    zf.write(path, arcname=str(path.relative_to(world)))
                except OSError:
                    continue
    return {"name": dest.name, "path": dest, "world": name,
            "size_bytes": dest.stat().st_size}
