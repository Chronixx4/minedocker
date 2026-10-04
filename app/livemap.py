"""Live-Weltkarte je Instanz über BlueMap (Mod bzw. Plugin von Modrinth).

- Einschalten lädt die passende BlueMap-Datei (+ Pflicht-Abhängigkeiten wie
  die Fabric API) in mods/ bzw. plugins/, bestätigt in core.conf den Download
  der Minecraft-Client-Ressourcen (accept-download — nur nach ausdrücklicher
  Zustimmung im Dashboard) und reserviert einen Host-Port für den BlueMap-
  Webserver (Container-Port 8100).
- Beim (Neu-)Start veröffentlicht runtime.start_instance den Port; vorher
  sorgt prepare_start() dafür, dass accept-download gesetzt ist und die Karte
  nach einem Welt-Wechsel neu gerendert wird.
- Ausschalten entfernt nur die BlueMap-Datei; gerenderte Kartendaten bleiben.
"""
import json
import logging
import re
import shutil
from pathlib import Path

import httpx
from fastapi import HTTPException

from . import instances, modrinth

logger = logging.getLogger("dashboard.livemap")

PROJECT = "bluemap"
WEB_PORT = 8100            # BlueMap-Webserver im Container (Standard)
PORT_OFFSET = 2000         # Host-Port = Spiel-Port + 2000 (RCON ist + 1000)

_PLUGIN_LOADERS = {"paper", "spigot", "bukkit"}
# Modrinth-Loader, unter denen BlueMap je Server-Typ zu finden ist
_LOADER_QUERY = {
    "fabric": ["fabric"],
    "quilt": ["quilt", "fabric"],
    "forge": ["forge"],
    "neoforge": ["neoforge"],
    "paper": ["paper", "spigot", "bukkit"],
    "spigot": ["spigot", "bukkit"],
    "bukkit": ["bukkit", "spigot"],
}

_CORE_CONF = """# Angelegt von Minedocker: Der Download der Minecraft-Client-Ressourcen
# (Texturen/Modelle, Mojang-EULA) wurde im Dashboard bestätigt.
accept-download: true
data: "bluemap"
render-thread-count: 1
scan-for-mod-resources: true
metrics: false
"""
_ACCEPT_RE = re.compile(r"^(\s*accept-download\s*[:=]\s*)false\b", re.M)


def supported(loader: str) -> bool:
    return loader in _LOADER_QUERY


def is_plugin(instance: dict) -> bool:
    return instance.get("loader") in _PLUGIN_LOADERS


def settings_of(instance: dict) -> dict:
    data = instance.get("map")
    return data if isinstance(data, dict) else {}


def enabled(instance: dict) -> bool:
    return bool(settings_of(instance).get("enabled"))


def map_port(instance: dict) -> int | None:
    try:
        return int(settings_of(instance).get("port") or 0) or None
    except (TypeError, ValueError):
        return None


def _target_dir(instance: dict) -> Path:
    base = instances.instance_dir(instance["id"])
    return base / ("plugins" if is_plugin(instance) else "mods")


def _config_dir(instance: dict) -> Path:
    base = instances.instance_dir(instance["id"])
    return base / "plugins" / "BlueMap" if is_plugin(instance) else base / "config" / "bluemap"


def _core_conf(instance: dict) -> Path:
    return _config_dir(instance) / "core.conf"


def _accept_download(instance: dict) -> None:
    """core.conf anlegen bzw. accept-download: false → true umstellen."""
    path = _core_conf(instance)
    try:
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            patched = _ACCEPT_RE.sub(r"\1true", text)
            if patched != text:
                path.write_text(patched, encoding="utf-8")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_CORE_CONF, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f"BlueMap-Konfiguration nicht schreibbar: {exc}") from exc


def _download_accepted(instance: dict) -> bool:
    try:
        text = _core_conf(instance).read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(re.search(r"^\s*accept-download\s*[:=]\s*true\b", text, re.M))


def _allocate_port(instance: dict) -> int:
    """Freier Host-Port für den Karten-Webserver (Spiel-Port + 2000 bevorzugt)."""
    existing = instances.list_instances()
    used = set()
    for other in existing:
        try:
            used.add(int(other["port"]))
            used.add(int(other.get("rcon_port") or int(other["port"]) + 1000))
        except (KeyError, TypeError, ValueError):
            continue
        if other.get("id") != instance["id"] and map_port(other):
            used.add(map_port(other))
    current = map_port(instance)
    if current and current not in used:
        return current
    start = int(instance["port"]) + PORT_OFFSET
    for candidate in range(start, min(start + 500, 65536)):
        if candidate not in used:
            return candidate
    raise HTTPException(status_code=500, detail="Kein freier Port für die Karte gefunden")


def _jar_stem(filename: str) -> str:
    """'fabric-api-0.119.2+1.21.4.jar' → 'fabric-api' (Vergleich ohne Version)."""
    return re.split(r"[-_+](?=v?\d)", filename.lower(), maxsplit=1)[0]


def _already_present(directory: Path, filename: str) -> bool:
    if (directory / filename).exists():
        return True
    stem = _jar_stem(filename)
    try:
        return any(_jar_stem(p.name) == stem for p in directory.glob("*.jar"))
    except OSError:
        return False


async def _resolve(instance: dict) -> list[dict]:
    """BlueMap-Datei (+ Pflicht-Abhängigkeiten bei Mods) für diese Instanz."""
    loaders = _LOADER_QUERY[instance["loader"]]
    game_version = instance["game_version"]
    async with httpx.AsyncClient(timeout=20.0) as client:
        versions = await modrinth._get_json(
            client, f"/project/{PROJECT}/version",
            params={"loaders": json.dumps(loaders),
                    "game_versions": json.dumps([game_version])})
    if not versions:
        raise HTTPException(
            status_code=404,
            detail=f"BlueMap gibt es (noch) nicht für {instance['loader']} {game_version}")
    version = versions[0]
    filename, url, size, sha1 = modrinth._pick_file_full(version)
    files = [{"filename": filename, "url": url, "size": size, "sha1": sha1, "main": True}]
    if not is_plugin(instance):
        deps = await modrinth.dependency_files(
            str(version.get("project_id") or PROJECT), str(version["id"]),
            loaders[-1] if instance["loader"] == "quilt" else loaders[0], game_version)
        for dep in deps.get("files") or []:
            files.append({**dep, "main": False})
    return files


async def enable(instance_id: str, accept_download: bool) -> dict:
    instance = instances.get_instance(instance_id)
    if not accept_download:
        raise HTTPException(
            status_code=400,
            detail="Bitte bestätigen, dass BlueMap die Minecraft-Client-Ressourcen "
                   "herunterladen darf (Mojang-EULA)")
    if not supported(instance.get("loader")):
        raise HTTPException(
            status_code=400,
            detail="Die Live-Karte braucht Fabric, Quilt, Forge, NeoForge, Paper, "
                   "Spigot oder Bukkit — Vanilla-Server können keine Mods laden")
    files = await _resolve(instance)
    target = _target_dir(instance)
    target.mkdir(parents=True, exist_ok=True)
    installed = []
    for entry in files:
        if not entry["main"] and _already_present(target, entry["filename"]):
            continue  # z. B. Fabric API schon vorhanden → keine Doppel-Mods
        if entry["main"]:
            # ältere BlueMap-Versionen ersetzen
            for old in target.glob("*.jar"):
                if _jar_stem(old.name) == "bluemap":
                    old.unlink(missing_ok=True)
        job = {"total": int(entry.get("size") or 0), "downloaded": 0}
        ok = await modrinth._download_one(job, entry["url"], target / entry["filename"],
                                          sha1=entry.get("sha1"))
        if not ok:
            raise HTTPException(status_code=502,
                                detail=f"Download fehlgeschlagen: {entry['filename']} "
                                       f"({job.get('error') or 'unbekannt'})")
        installed.append(entry["filename"])
    _accept_download(instance)
    # Metadaten frisch laden (Download kann dauern) und Karte eintragen
    with instances._LOCK:
        instance = instances.get_instance(instance_id)
        port = _allocate_port(instance)
        instance["map"] = {
            "enabled": True,
            "port": port,
            "file": files[0]["filename"],
            "world": _level_name(instance),
        }
        instances.update_instance(instance)
    instances.invalidate_disk_cache(instance_id)
    logger.info("Live-Karte aktiviert: %s (Port %s, %s)", instance_id, port,
                ", ".join(installed) or "bereits installiert")
    return status(instance) | {"installed": installed}


def disable(instance_id: str) -> dict:
    with instances._LOCK:
        instance = instances.get_instance(instance_id)
        data = settings_of(instance)
        target = _target_dir(instance)
        try:
            for jar in target.glob("*.jar"):
                if _jar_stem(jar.name) == "bluemap":
                    jar.unlink(missing_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=500,
                                detail=f"BlueMap-Datei nicht entfernbar: {exc}") from exc
        instance["map"] = {**data, "enabled": False}
        instances.update_instance(instance)
    logger.info("Live-Karte deaktiviert: %s", instance_id)
    return status(instance)


def _level_name(instance: dict) -> str:
    from . import worlds
    return worlds._active_level_name(instances.instance_dir(instance["id"]))


def prepare_start(instance: dict) -> None:
    """Vor jedem Container-Start: accept-download sicherstellen und nach einem
    Welt-Wechsel die Karten-Konfiguration/-Daten zurücksetzen (BlueMap legt
    sie für die neue Welt automatisch neu an und rendert neu)."""
    if not enabled(instance):
        return
    try:
        _accept_download(instance)
    except HTTPException as exc:
        logger.warning("BlueMap-Konfiguration: %s", exc.detail)
    data = settings_of(instance)
    current = _level_name(instance)
    if data.get("world") and data.get("world") != current:
        base = instances.instance_dir(instance["id"])
        for path in (_config_dir(instance) / "maps", base / "bluemap" / "web" / "maps"):
            shutil.rmtree(path, ignore_errors=True)
        logger.info("Live-Karte: Welt gewechselt (%s → %s), Karte wird neu gerendert",
                    data.get("world"), current)
    if data.get("world") != current:
        with instances._LOCK:
            fresh = instances.get_instance(instance["id"])
            fresh["map"] = {**settings_of(fresh), "world": current}
            instances.update_instance(fresh)


def status(instance: dict) -> dict:
    data = settings_of(instance)
    target = _target_dir(instance)
    try:
        file_present = any(_jar_stem(p.name) == "bluemap" for p in target.glob("*.jar"))
    except OSError:
        file_present = False
    return {
        "supported": supported(instance.get("loader")),
        "enabled": bool(data.get("enabled")),
        "port": map_port(instance),
        "file": data.get("file"),
        "installed": file_present,
        "download_accepted": _download_accepted(instance),
        "kind": "plugin" if is_plugin(instance) else "mod",
    }


def _probe_url(instance: dict) -> str | None:
    from . import runtime
    if Path("/.dockerenv").exists():
        return f"http://{runtime.container_name(instance['id'])}:{WEB_PORT}/settings.json"
    port = map_port(instance)
    return f"http://127.0.0.1:{port}/settings.json" if port else None


async def reachable(instance: dict) -> bool:
    """True, wenn der BlueMap-Webserver der Instanz antwortet."""
    url = _probe_url(instance)
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False
