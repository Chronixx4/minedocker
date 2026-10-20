"""Mod-Sync für Spieler über AutoModpack (Modrinth: automodpack).

Die Mod liegt auf Server UND Client. Der Server veröffentlicht seinen
mods/-Ordner (reine Server-Mods werden anhand der Metadaten weggelassen),
Clients gleichen beim Join ab: neue Mods kommen dazu, vom Server entfernte
verschwinden wieder. Dazu kommen Client-only-Mods aus
automodpack/host-modpack/main/mods/ (laufen nicht auf dem Server).

Das Config-Format von AutoModpack unterscheidet sich je nach Version
(server.conf im HOCON-Stil, ältere Versionen automodpack-server.json); das
Dashboard ändert deshalb nur zwei Dinge zeilenweise: den Schalter „Mod Pflicht“
und die Zeile `exclude: [...]` (Mods, die nicht an Spieler verteilt werden).
Alles andere in der Config bleibt unangetastet; ist die Struktur nicht
eindeutig (z. B. mehrere exclude-Zeilen), lehnt das Dashboard die Änderung ab.
"""
import json
import re
from pathlib import Path

from fastapi import HTTPException

from . import instances
from .security import validate_filename

PROJECT_ID = "k68glP2e"  # Modrinth: automodpack
LOADERS = ("fabric", "forge", "neoforge", "quilt")

_REQUIRE_RE = re.compile(r"^(\s*)require-modpack(\s*[:=]\s*)\S+", re.MULTILINE)
_EXCLUDE_RE = re.compile(r"^(?P<pre>[ \t]*exclude[ \t]*[:=][ \t]*)\[(?P<body>.*)\](?P<post>[ \t]*)$",
                         re.MULTILINE)
_BAD_NAME_CHARS = set('"\'[]{},\\*?!#')
_JAR_RE = re.compile(r"^automodpack[-_.\w]*\.jar(\.disabled)?$", re.IGNORECASE)


def _base(instance_id: str) -> Path:
    return instances.instance_dir(instance_id) / "automodpack"


def conf_path(instance_id: str) -> Path:
    return _base(instance_id) / "server.conf"


def legacy_conf_path(instance_id: str) -> Path:
    return _base(instance_id) / "automodpack-server.json"


def client_mods_dir(instance_id: str) -> Path:
    return _base(instance_id) / "host-modpack" / "main" / "mods"


def find_jar(instance_id: str) -> str | None:
    mods = instances.mods_dir(instance_id)
    if not mods.is_dir():
        return None
    for entry in sorted(mods.iterdir()):
        if entry.is_file() and _JAR_RE.match(entry.name):
            return entry.name
    return None


def read_require(instance_id: str) -> bool | None:
    """Ist die Mod für Spieler Pflicht? None = Config (noch) nicht vorhanden."""
    conf = conf_path(instance_id)
    if conf.is_file():
        match = re.search(r"^\s*require-modpack\s*[:=]\s*(\S+)", conf.read_text(
            encoding="utf-8", errors="replace"), re.MULTILINE)
        return match.group(1).strip('"').lower() == "true" if match else None
    legacy = legacy_conf_path(instance_id)
    if legacy.is_file():
        try:
            value = json.loads(legacy.read_text(encoding="utf-8")).get("requireAutoModpackOnClient")
        except (OSError, ValueError, AttributeError):
            return None
        return value if isinstance(value, bool) else None
    return None


def set_require(instance_id: str, required: bool) -> bool:
    conf = conf_path(instance_id)
    if conf.is_file():
        text = conf.read_text(encoding="utf-8", errors="replace")
        new, count = _REQUIRE_RE.subn(
            lambda m: f"{m.group(1)}require-modpack{m.group(2)}{'true' if required else 'false'}",
            text, count=1)
        if not count:
            raise HTTPException(status_code=409,
                                detail="Option require-modpack nicht in der Config gefunden")
        conf.write_text(new, encoding="utf-8")
        return required
    legacy = legacy_conf_path(instance_id)
    if legacy.is_file():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(status_code=409, detail="Config nicht lesbar")
        if not isinstance(data, dict) or "requireAutoModpackOnClient" not in data:
            raise HTTPException(status_code=409,
                                detail="Option requireAutoModpackOnClient nicht gefunden")
        data["requireAutoModpackOnClient"] = required
        legacy.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return required
    raise HTTPException(status_code=409,
                        detail="Config noch nicht erzeugt — Server einmal mit AutoModpack starten")


def list_client_mods(instance_id: str) -> list[dict]:
    folder = client_mods_dir(instance_id)
    if not folder.is_dir():
        return []
    return [{"name": p.name, "size": p.stat().st_size}
            for p in sorted(folder.iterdir()) if p.is_file() and p.suffix == ".jar"]


def client_mod_path(instance_id: str, filename: str) -> Path:
    validate_filename(filename)
    folder = client_mods_dir(instance_id)
    path = (folder / filename).resolve()
    if path.parent != folder.resolve():
        raise HTTPException(status_code=400, detail="Pfadmanipulation erkannt")
    return path


def delete_client_mod(instance_id: str, filename: str) -> None:
    path = client_mod_path(instance_id, filename)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Datei nicht gefunden")
    path.unlink()


def status(instance: dict) -> dict:
    instance_id = instance["id"]
    jar = find_jar(instance_id)
    conf = conf_path(instance_id).is_file() or legacy_conf_path(instance_id).is_file()
    return {
        "supported": instance.get("loader") in LOADERS,
        "loader": instance.get("loader"),
        "installed": jar is not None,
        "jar": jar,
        "config": conf,
        "require": read_require(instance_id),
        "client_mods": list_client_mods(instance_id),
        "server_mods": server_mods(instance_id) if jar else [],
        "distribute_editable": excluded_paths(instance_id) is not None,
        "install_item": {"source": "modrinth", "project_id": PROJECT_ID},
    }


def _split_items(body: str) -> list[str]:
    """Rohe Listeneinträge trennen; Kommas in {…} oder "…" trennen nicht."""
    items, cur, depth, quote = [], [], 0, False
    for ch in body:
        if ch == '"':
            quote = not quote
        elif not quote and ch == "{":
            depth += 1
        elif not quote and ch == "}":
            depth = max(0, depth - 1)
        if ch == "," and not quote and depth == 0:
            items.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        items.append("".join(cur).strip())
    return [i for i in items if i]


def _unquote(raw: str) -> str:
    return raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] == '"' else raw


def _exclude_match(instance_id: str):
    """(Pfad, Text, Match) der einzigen exclude-Zeile; None, wenn nicht eindeutig."""
    conf = conf_path(instance_id)
    if not conf.is_file():
        return None
    text = conf.read_text(encoding="utf-8", errors="replace")
    found = list(_EXCLUDE_RE.finditer(text))
    if len(found) != 1:
        return None
    return conf, text, found[0]


def excluded_paths(instance_id: str) -> set[str] | None:
    """Ausgenommene Pfade; None = nicht bearbeitbar (Config fehlt/mehrdeutig)."""
    found = _exclude_match(instance_id)
    if found is None:
        return None
    return {_unquote(i) for i in _split_items(found[2].group("body"))}


def set_distribute(instance_id: str, filename: str, distribute: bool) -> bool:
    """Mod an Spieler verteilen (True) oder ausnehmen (False)."""
    validate_filename(filename)
    if any(c in _BAD_NAME_CHARS for c in filename):
        raise HTTPException(status_code=400,
                            detail="Dateiname enthält Sonderzeichen — bitte Config von Hand anpassen")
    mods = instances.mods_dir(instance_id)
    if not ((mods / filename).is_file()):
        raise HTTPException(status_code=404, detail="Mod nicht gefunden")
    if _JAR_RE.match(filename):
        raise HTTPException(status_code=400, detail="AutoModpack selbst wird nie verteilt")
    found = _exclude_match(instance_id)
    if found is None:
        raise HTTPException(
            status_code=409,
            detail="Config noch nicht erzeugt oder exclude-Zeile nicht eindeutig — "
                   "Server einmal mit AutoModpack starten")
    conf, text, match = found
    entry = f"mods/{filename}"
    raws = _split_items(match.group("body"))
    kept = [r for r in raws if _unquote(r) != entry]
    if not distribute:
        quoted = not raws or any(r.startswith('"') for r in raws)
        kept.append(f'"{entry}"' if quoted else entry)
    if kept == raws:
        return distribute
    backup = conf.with_name(conf.name + ".dashboard-bak")
    if not backup.exists():
        backup.write_text(text, encoding="utf-8")
    new_line = f"{match.group('pre')}[{', '.join(kept)}]{match.group('post')}"
    conf.write_text(text[:match.start()] + new_line + text[match.end():], encoding="utf-8")
    return distribute


def server_mods(instance_id: str) -> list[dict]:
    excluded = excluded_paths(instance_id)
    result = []
    for mod in instances.list_mods(instance_id):
        if not mod["enabled"] or _JAR_RE.match(mod["filename"]):
            continue
        result.append({"filename": mod["filename"],
                       "distribute": None if excluded is None
                       else f"mods/{mod['filename']}" not in excluded})
    return result
