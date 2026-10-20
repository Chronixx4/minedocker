"""Mod-Sync für Spieler über AutoModpack (Modrinth: automodpack).

Die Mod liegt auf Server UND Client. Der Server veröffentlicht seinen
mods/-Ordner (reine Server-Mods werden anhand der Metadaten weggelassen),
Clients gleichen beim Join ab: neue Mods kommen dazu, vom Server entfernte
verschwinden wieder. Dazu kommen Client-only-Mods aus
automodpack/host-modpack/main/mods/ (laufen nicht auf dem Server).

Das Config-Format von AutoModpack unterscheidet sich je nach Version
(server.conf im HOCON-Stil, ältere Versionen automodpack-server.json); das
Dashboard ändert deshalb nur den einen Schalter „Mod Pflicht“ zeilenweise
und fasst sonst keine Config an.
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
        "install_item": {"source": "modrinth", "project_id": PROJECT_ID},
    }
