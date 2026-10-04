"""Modpack-Installer: Modrinth-Modpacks (.mrpack) herunterladen, prüfen, installieren.

Ablauf eines Install-Jobs:
  1. .mrpack-Archive vom Modrinth-CDN laden (Fortschritt im Job)
  2. modrinth.index.json entpacken und Kompatibilität prüfen
     (Minecraft-Version + Loader-Abhängigkeit der Instanz)
  3. Speicherplatz-Prüfung
  4. Alle Dateien des Modpacks parallel laden (SHA1-Prüfung, Pfad-Schutz)
  5. Instanz-Metadaten aktualisieren (Modpack-Info + Loader-Version)

Fehlerfälle (landen alle im Job als status="error"):
  - Download fehlgeschlagen / unvollständig / Hash-Mismatch
  - inkompatible Version-/Loader-Kombination
  - zu wenig Speicherplatz (vorab und während der Installation geprüft)
"""
import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path

import httpx
from fastapi import HTTPException

from . import modrinth
from .config import ALLOWED_LOADERS, settings
from .curseforge import DOWNLOAD_URL_TEMPLATE as _CF_DL_URL
from .instances import create_instance, get_instance, instance_dir, list_instances, pack_dir, update_instance
from .security import validate_curseforge_download_url, validate_identifier

# Nur diese Top-Level-Ordner dürfen aus einem Modpack beschrieben werden
_ALLOWED_TOP_DIRS = ("mods", "resourcepacks", "shaderpacks", "config",
                     "defaultconfigs", "kubejs", "openloader")
_CONCURRENCY = 4  # gleichzeitige Mod-Downloads
_SPACE_BUFFER = 512 * 1024 * 1024  # Sicherheitspuffer 512 MiB
_MAX_PACK_BYTES = 4 * 1024 * 1024 * 1024  # harte Grenze: 4 GiB

# Transiente CurseForge-Fehler (Website-Redirect hinter Cloudflare) werden
# mit steigender Pause wiederholt — ein einziger 403/429 soll die ganze
# Pack-Installation nicht abbrechen.
_CF_RETRY_STATUS = frozenset({403, 408, 425, 429, 500, 502, 503, 504})
_CF_RETRIES = 2        # zusätzliche Versuche nach dem ersten
_CF_RETRY_DELAY = 1.0  # Basis-Pause in Sekunden (skaliert mit Versuchsnr.)

# Bekannte Client-only-Rendermods, die einen Server beim Start abschießen
# (Sodium & Co. greifen beim Pre-Launch nach LWJGL — existiert serverseitig
# nicht). CurseForge-Manifeste enthalten keine Server/Client-Info und
# neoforge.mods.toml keinen Seiten-Marker für die Mod selbst, daher kann
# das nur über eine Muster-Liste gelöst werden. Liste = häufige Crasher
# plus strikt client-seitige UI-/Render-Mods (Skip ist für Server immer
# ungefährlich); zusätzliche Muster via CF_CLIENT_ONLY_MODS.
_CLIENT_ONLY_PATTERNS = (
    # Render-/Performance-Mods mit Server-Crash
    "sodium",      # auch sodium-extra, reeses-sodium-options, sodiumoptionsapi
    "embeddium",   # auch embeddium-plus/-extras
    "rubidium",    # auch rubidium-extra
    "magnesium",
    "oculus",
    "iris",
    "nvidium",
    # Strikt client-seitige UI-/QoL-Mods (nutzlos auf Servern, teils Crasher)
    "darkmodeeverywhere", "dark-mode-everywhere",
    "rebindnarrator", "rebind-narrator", "rebind_narrator",
    "toastcontrol", "toast-control",
    "mousetweaks", "mouse-tweaks",
    "inventoryprofilesnext", "inventory-profiles-next",
    "inventoryessentials", "inventory-essentials",
    "3dskinlayers", "3d-skin-layers",
    "notenoughanimations", "not-enough-animations",
    "dynamicfps", "dynamic-fps",
    "controlling",
    "cullleaves", "cull-leaves", "culllessleaves", "cull-less-leaves",
    "physics-mod", "physicsmod",
)

# Loader-Präferenz bei mrpack-Versionen mit mehreren Loadern
_LOADER_PREFERENCE = ("fabric", "forge", "neoforge", "quilt")
_MODRINTH_GAME_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?(-rc\d+|-pre\d+)?$")
# Zeichensatz für Instanznamen (siehe instances._NAME_RE)
_NAME_CLEAN_RE = re.compile(r"[^A-Za-z0-9_.\- ]")


def _headers() -> dict:
    return {"User-Agent": settings.user_agent, "Accept": "application/json"}


def _new_client(**kwargs) -> httpx.AsyncClient:
    """Client-Fabrik (Tests injizieren hier einen MockTransport)."""
    kwargs.setdefault("timeout", httpx.Timeout(30.0, read=300.0))
    kwargs.setdefault("follow_redirects", True)
    return httpx.AsyncClient(**kwargs)


def _validate_id(value: str, what: str) -> str:
    from .security import validate_identifier
    return validate_identifier(value, what)


def _running_pack_job(instance_id: str):
    for job in modrinth.JOBS.values():
        if (job.get("kind") == "pack" and job.get("instance_id") == instance_id
                and job.get("status") in ("downloading", "installing")):
            return job
    return None


async def _modrinth_pack_search_raw(query: str, offset: int = 0,
                                    loader: str = None,
                                    game_version: str = None) -> tuple:
    """Modrinth-Modpack-Suche; liefert (total, normalisierte Treffer ohne
    Kompatibilitäts-Markierung). Optional nach Loader und MC-Version filtern."""
    facets = [["project_type:modpack"]]
    if game_version:
        facets.append([f"versions:{game_version}"])
    if loader:
        facets.append([f"categories:{loader}"])
    params: dict[str, str | int] = {"limit": 20, "offset": max(0, min(offset, 1000)),
                                    "index": "relevance", "facets": json.dumps(facets)}
    if (query or "").strip():
        params["query"] = query.strip()[:100]
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{settings.modrinth_api}/search",
                                    params=params, headers=_headers())
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502,
                            detail=f"Modrinth nicht erreichbar ({exc.__class__.__name__})")
    if resp.status_code != 200:
        raise HTTPException(status_code=502,
                            detail=f"Modrinth antwortete mit HTTP {resp.status_code}")
    data = resp.json()
    hits = []
    for h in data.get("hits", []):
        hits.append({
            "project_id": h.get("project_id") or "",
            "slug": h.get("slug") or "",
            "title": h.get("title") or "",
            "description": h.get("description") or "",
            "downloads": int(h.get("downloads") or 0),
            "icon_url": h.get("icon_url") or "",
            "loaders": [str(x).lower() for x in (h.get("loaders") or [])],
            "versions": [str(x) for x in (h.get("versions") or [])],
        })
    return int(data.get("total") or len(hits)), hits


async def search_modpacks_global(query: str, offset: int = 0,
                                 loader: str = None,
                                 game_version: str = None) -> dict:
    """Globale Modpack-Suche ohne Instanzbezug (für 'Server aus Modpack
    erstellen'); die Kompatibilität eines Packs zeigt das Frontend aus
    loaders/versions selbst an."""
    total, hits = await _modrinth_pack_search_raw(query, offset,
                                                  loader=loader,
                                                  game_version=game_version)
    for h in hits:
        h["compatible"] = None
    return {"total": total, "hits": hits, "source": "modrinth"}


async def list_pack_versions(source: str, project_id: str) -> dict:
    """Alle Versionen eines Modpacks (für die Versionswahl beim
    Server-Erstellen): Modrinth-Versionen bzw. CurseForge-Pack-Dateien."""
    _validate_id(project_id, "Projekt-ID")
    if source == "curseforge":
        from . import curseforge  # lazy, vermeidet Import-Zirkel
        curseforge._require_key()
        return await curseforge.list_pack_versions(project_id)
    async with _new_client() as client:
        versions = await modrinth._get_json(client, f"/project/{project_id}/version")
    out = []
    for v in versions or []:
        if not isinstance(v, dict) or not v.get("id"):
            continue
        out.append({
            "version_id": str(v.get("id")),
            "name": str(v.get("name") or v.get("version_number") or v.get("id")),
            "loaders": [str(x).lower() for x in (v.get("loaders") or [])],
            "game_versions": [str(x) for x in (v.get("game_versions") or [])],
            "date": str(v.get("date_published") or ""),
            "size": int((_primary_modrinth_file(v) or {}).get("size") or 0),
            "featured": bool(v.get("featured")),
        })
    return {"source": "modrinth", "project_id": project_id, "versions": out}


async def search_modpacks(instance_id: str, query: str, offset: int = 0) -> dict:
    """Sucht Modpacks auf Modrinth und markiert, welche zur Instanz passen."""
    instance = get_instance(instance_id)
    total, hits = await _modrinth_pack_search_raw(query, offset)
    for h in hits:
        h["compatible"] = (instance["loader"] in h["loaders"]
                           and instance["game_version"] in h["versions"])
    return {
        "total": total,
        "hits": hits,
        "instance": {"id": instance["id"], "loader": instance["loader"],
                     "game_version": instance["game_version"]},
    }


def _pick_pack_file(version: dict):
    """Primäre .mrpack-Datei einer Modpack-Version wählen."""
    files = version.get("files") or []
    primary = next((f for f in files if f.get("primary")), None)
    if primary is None:
        candidates = [f for f in files
                      if str(f.get("filename") or "").lower().endswith(".mrpack")]
        primary = max(candidates, key=lambda f: f.get("size") or 0) if candidates else None
    if not primary or not primary.get("url"):
        raise HTTPException(status_code=404, detail="Keine .mrpack-Datei gefunden")
    return (str(primary["filename"]), str(primary["url"]), int(primary.get("size") or 0))


async def _resolve_pack_version(client: httpx.AsyncClient, instance: dict,
                                project_id: str, version_id):
    """Modpack-Version auflösen; ohne version_id die neueste kompatible wählen."""
    if version_id:
        version = await modrinth._get_json(client, f"/version/{version_id}")
        if version.get("project_id") != project_id:
            raise HTTPException(status_code=400,
                                detail="Versions-ID gehört nicht zum Projekt")
        return version
    versions = await modrinth._get_json(client, f"/project/{project_id}/version")
    version = _first_compatible_modrinth_version(versions, instance["loader"],
                                                 instance["game_version"])
    if version is None:
        raise HTTPException(
            status_code=409,
            detail=(f"Keine Modpack-Version kompatibel mit "
                    f"{instance['loader']} {instance['game_version']}"))
    return version


def _check_compatibility(instance: dict, pack: dict) -> dict:
    """Prüft MC-Version und Loader-Abhängigkeit (normalisiertes Index-Format);
    gibt die Loader-Abhängigkeit zurück."""
    if not isinstance(pack, dict) or not (pack.get("files") or pack.get("server_pack")):
        raise HTTPException(status_code=400, detail="Modpack-Index ist leer oder beschädigt")
    pack_game_version = pack.get("game_version")
    if not pack_game_version:
        raise HTTPException(status_code=400,
                            detail="Modpack gibt keine Minecraft-Version an")
    if pack_game_version != instance["game_version"]:
        raise HTTPException(
            status_code=400,
            detail=(f"Inkompatibel: Modpack benötigt Minecraft {pack_game_version}, "
                    f"Instanz läuft mit {instance['game_version']}"))
    required_loader = pack.get("loader")
    required_version = str(pack.get("loader_version") or "")
    if required_loader is None:
        raise HTTPException(status_code=400,
                            detail="Modpack deklariert keinen unterstützten Loader")
    if required_loader != instance["loader"]:
        raise HTTPException(
            status_code=400,
            detail=(f"Inkompatibel: Modpack benötigt Loader '{required_loader}', "
                    f"Instanz-Loader ist '{instance['loader']}'. "
                    f"Bukkit-basierte Server (paper/spigot/bukkit) können keine "
                    f"Mod-/Modpack-Dateien aus Modrinth laden."))
    return {"loader": required_loader, "version": required_version,
            "game_version": pack_game_version}


def _needs_version_adaptation(instance: dict, pack: dict) -> bool:
    """True, wenn das Modpack eine andere MC-/Loader-Kombination verlangt als
    die Instanz hat (beide Pack-Angaben müssen vorhanden und gültig sein —
    sonst übernimmt _check_compatibility die Fehlermeldung)."""
    pack_loader = str(pack.get("loader") or "").strip().lower()
    pack_game = str(pack.get("game_version") or "").strip()
    if not pack_loader or not pack_game or pack_loader not in ALLOWED_LOADERS:
        return False
    return (pack_game != instance.get("game_version")
            or pack_loader != instance.get("loader"))


def _adapt_instance_for_pack(instance: dict, pack: dict) -> dict:
    """Stellt eine Instanz automatisch auf die vom Modpack benötigte
    Minecraft-/Loader-Kombination um (Auto-Erkennung beim Upload): Die
    Metadaten werden vor der Installation angepasst, der Container bezieht
    beim nächsten Start die passende Server-Software.

    Rückgabe wie _check_compatibility (loader/version/game_version) plus
    from/to für den Hinweis im Job. Fehler: 400 bei nicht für Server
    unterstütztem Loader, 409 wenn die Instanz läuft (Umstellung erfordert
    einen Stopp).
    """
    pack_loader = str(pack.get("loader") or "").strip().lower()
    if pack_loader not in ALLOWED_LOADERS:
        raise HTTPException(
            status_code=400,
            detail=(f"Modpack-Loader '{pack_loader or '?'}' wird für Server "
                    f"nicht unterstützt ({', '.join(ALLOWED_LOADERS)})"))
    pack_game = str(pack.get("game_version") or "").strip()
    old_loader = instance.get("loader")
    old_game = instance.get("game_version")
    from . import runtime  # lazy, vermeidet Import-Zirkel
    if runtime.is_running(instance):
        raise HTTPException(
            status_code=409,
            detail=(f"Instanz läuft — bitte zuerst stoppen, damit sie auf das "
                    f"Modpack umgestellt werden kann "
                    f"({old_loader} {old_game} → {pack_loader} {pack_game})"))
    instance["game_version"] = validate_identifier(pack_game, "Minecraft-Version")
    instance["loader"] = pack_loader
    loader_version = str(pack.get("loader_version") or "").strip()
    if loader_version:
        instance["loader_version"] = loader_version
    update_instance(instance)
    return {"loader": pack_loader, "version": loader_version,
            "game_version": pack_game,
            "from": {"loader": old_loader, "game_version": old_game},
            "to": {"loader": pack_loader, "game_version": pack_game}}


def _safe_pack_path(root: Path, rel_path: str) -> Path:
    """Löst einen Modpack-Pfad auf; verbietet Traversal und unerlaubte Ordner."""
    rel = str(rel_path or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/") or ":" in rel or "\x00" in rel:
        raise HTTPException(status_code=400,
                            detail=f"Unerlaubter Pfad im Modpack: {rel_path!r}")
    top = rel.split("/", 1)[0].lower()
    if top not in _ALLOWED_TOP_DIRS:
        return root / "__skipped__" / rel  # wird vom Aufrufer übersprungen
    return root / rel


def _check_disk_space(target: Path, needed: int) -> None:
    """Speicherplatz prüfen; 507 (Insufficient Storage) bei Mangel."""
    try:
        usage = shutil.disk_usage(target if target.exists() else target.parent)
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f"Speicherplatz nicht ermittelbar: {exc}")
    needed_total = needed + _SPACE_BUFFER
    if usage.free < needed_total:
        raise HTTPException(
            status_code=507,
            detail=(f"Nicht genug Speicherplatz: benötigt ≥ {needed_total // (1024**2)} MiB, "
                    f"frei sind {usage.free // (1024**2)} MiB"))


def _check_disk_space_job(target: Path, needed: int) -> None:
    try:
        usage = shutil.disk_usage(target if target.exists() else target.parent)
    except OSError as exc:
        raise RuntimeError(f"Speicherplatz nicht ermittelbar: {exc}")
    if usage.free < needed + _SPACE_BUFFER:
        raise RuntimeError(
            f"Nicht genug Speicherplatz: benötigt ≥ {(needed + _SPACE_BUFFER) // (1024**2)} MiB, "
            f"frei sind {usage.free // (1024**2)} MiB")


def _extract_index(pack_path: Path) -> tuple:
    """Liest den Index eines Modrins- oder CurseForge-Modpacks.

    Rückgabe: (format, roh_index) mit format in ("modrinth", "curseforge").
    """
    try:
        with zipfile.ZipFile(pack_path) as archive:
            names = set(archive.namelist())
            if "modrinth.index.json" in names:
                with archive.open("modrinth.index.json") as fh:
                    return "modrinth", json.loads(fh.read().decode("utf-8"))
            if "manifest.json" in names:
                with archive.open("manifest.json") as fh:
                    return "curseforge", json.loads(fh.read().decode("utf-8"))
            server_pack = _detect_server_pack(archive)
            if server_pack is not None:
                return "serverpack", server_pack
            if any(n.lower().endswith((".zip", ".mrpack")) for n in names):
                raise HTTPException(
                    status_code=400,
                    detail=("Im Archiv steckt ein weiteres Archiv — bitte die "
                            "innere .zip/.mrpack direkt hochladen"))
            raise HTTPException(
                status_code=400,
                detail=("Kein Modpack erkannt: weder manifest.json "
                        "(CurseForge-Export), modrinth.index.json (Modrinth) "
                        "noch ein mods/-Ordner (Server Files) gefunden"))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise HTTPException(status_code=400,
                            detail=f"Modpack-Archiv beschädigt: {exc}") from exc
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f"Modpack nicht entpackbar: {exc}") from exc


# ---------------------------------------------------------------------------
# Server Files (fertiger Server-Ordner als Zip, z. B. ATM10 "ServerFiles-x.zip")
# ---------------------------------------------------------------------------

# Zusätzlich zu _ALLOWED_TOP_DIRS übernommene Ordner aus Server Files
_SERVER_PACK_EXTRA_DIRS = ("scripts", "global_packs", "paxi")
_SERVER_PACK_SCAN_JARS = 40          # Fallback: so viele Mods werden gelesen
_SERVER_PACK_MAX_JAR = 32 * 1024 * 1024
_LOADER_NAMES = {"neoforge": "neoforge", "forge": "forge",
                 "fabric": "fabric", "quilt": "quilt"}
_MC_VERSION_RE = re.compile(r"(?:1|2\d)\.\d+(?:\.\d+)?")
_NEOFORGE_INSTALLER_RE = re.compile(
    r"^neoforge-(\d+)\.(\d+)\.(\d+[\w.\-]*?)-installer\.jar$", re.I)
_FORGE_JAR_RE = re.compile(
    r"^forge-(1\.\d+(?:\.\d+)?)-([\d.]+)(?:-(?:installer|universal|server|shim))?\.jar$", re.I)
_FABRIC_LAUNCHER_RE = re.compile(
    r"^fabric-server-mc\.(1\.\d+(?:\.\d+)?)-loader\.([\d.]+)-launcher\.[\d.]+\.jar$", re.I)


def _server_pack_prefix(names: list) -> str | None:
    """Wurzel des Server-Ordners im Zip: "" oder genau ein Unterordner
    (z. B. "Server-Files-2.1/"), in dem ein mods/-Ordner mit .jar liegt."""
    prefixes = set()
    for name in names:
        parts = name.split("/")
        if len(parts) >= 2 and parts[-1].lower().endswith(".jar"):
            if parts[0].lower() == "mods":
                prefixes.add("")
            elif len(parts) >= 3 and parts[1].lower() == "mods":
                prefixes.add(parts[0] + "/")
    if "" in prefixes:
        return ""
    return sorted(prefixes)[0] if len(prefixes) == 1 else None


def _neoforge_mc(version: str) -> str | None:
    """NeoForge-Version → Minecraft-Version.

    20.2 bis 21.x: 21.1.x → 1.21.1, 21.0.x → 1.21. Ab Minecraft 26.1
    (Jahres-Versionen) beginnt die NeoForge-Version mit der MC-Version:
    26.1.0.x → 26.1, 26.1.1.x → 26.1.1. Ältere (47.x für 1.20.1) tragen
    die MC-Version selbst im Dateinamen → None."""
    parts = str(version or "").split("-", 1)[0].split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    major, minor = int(parts[0]), int(parts[1])
    if 20 <= major <= 21:
        return f"1.{major}" if minor == 0 else f"1.{major}.{minor}"
    if 26 <= major < 40 and len(parts) >= 4:
        patch = parts[2]
        return f"{major}.{minor}" if patch in ("", "0") else f"{major}.{minor}.{patch}"
    return None


def _parse_variables(text: str) -> dict:
    """KEY=VALUE-Zeilen (variables.txt der ATM-/All-the-Mods-Server-Files)."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip().upper()] = value.strip().strip("\"'")
    return out


def _server_pack_from_files(archive: zipfile.ZipFile, prefix: str,
                            names: list) -> dict:
    """Loader/MC-Version aus Begleitdateien der Server Files (ohne Mods zu lesen)."""
    found: dict = {}

    def read_text(rel: str, limit: int = 256 * 1024) -> str | None:
        try:
            info = archive.getinfo(prefix + rel)
        except KeyError:
            return None
        if info.file_size > limit:
            return None
        with archive.open(info) as fh:
            return fh.read().decode("utf-8", "replace")

    # 1) variables.txt (ATM-Server-Files, ServerPackCreator)
    text = read_text("variables.txt")
    if text:
        var = _parse_variables(text)
        loader = _LOADER_NAMES.get(var.get("MODLOADER", "").lower())
        if loader:
            found = {"loader": loader,
                     "loader_version": var.get("MODLOADER_VERSION") or None,
                     "game_version": var.get("MINECRAFT_VERSION") or None,
                     "detected_by": "variables.txt"}
            if found["game_version"]:
                return found

    # 2) ServerStarter (server-setup-config.yaml, ältere ATM-Packs)
    text = read_text("server-setup-config.yaml")
    if text:
        mc = re.search(r"^\s*mcVersion:\s*['\"]?([\d.]+)", text, re.M)
        lv = re.search(r"^\s*loaderVersion:\s*['\"]?([\w.\-]+)", text, re.M)
        url = re.search(r"^\s*installerUrl:\s*['\"]?(\S+)", text, re.M)
        url_text = (url.group(1).lower() if url else "")
        loader = ("neoforge" if "neoforge" in url_text else
                  "fabric" if "fabric" in url_text else
                  "forge" if "forge" in url_text else None)
        if loader and mc:
            return {"loader": loader,
                    "loader_version": lv.group(1) if lv else None,
                    "game_version": mc.group(1),
                    "detected_by": "server-setup-config.yaml"}

    # 3) Installer-/Launcher-Jars im Server-Ordner
    for name in names:
        rel = name[len(prefix):]
        if "/" in rel:
            continue
        m = _NEOFORGE_INSTALLER_RE.match(rel)
        if m:
            version = f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
            installer_mc = _neoforge_mc(version)
            if installer_mc:
                return {"loader": "neoforge", "loader_version": version,
                        "game_version": installer_mc, "detected_by": rel}
        m = _FORGE_JAR_RE.match(rel)
        if m:
            return {"loader": "forge", "loader_version": m.group(2),
                    "game_version": m.group(1), "detected_by": rel}
        m = _FABRIC_LAUNCHER_RE.match(rel)
        if m:
            return {"loader": "fabric", "loader_version": m.group(2),
                    "game_version": m.group(1), "detected_by": rel}

    # 4) libraries/ (bereits installierter Server)
    lib = prefix + "libraries/"
    neo, forge, fabric_loader, intermediary, mc_server = None, None, None, None, None
    for name in names:
        if not name.startswith(lib):
            continue
        parts = name[len(lib):].split("/")
        if len(parts) < 4:
            continue
        group = "/".join(parts[:3])
        if group == "net/neoforged/neoforge":
            neo = parts[3]
        elif group in ("net/minecraftforge/forge", "net/neoforged/forge") and "-" in parts[3]:
            forge = (parts[3], "neoforge" if "neoforged" in group else "forge")
        elif group == "net/fabricmc/fabric-loader":
            fabric_loader = parts[3]
        elif group == "net/fabricmc/intermediary":
            intermediary = parts[3]
        elif group == "net/minecraft/server" and "-" in parts[3]:
            mc_server = parts[3].split("-", 1)[0]
    if neo:
        neo_mc = _neoforge_mc(neo) or mc_server
        if neo_mc:
            return {"loader": "neoforge", "loader_version": neo,
                    "game_version": neo_mc, "detected_by": "libraries/"}
    if forge:
        mc, version = forge[0].split("-", 1)
        return {"loader": forge[1], "loader_version": version,
                "game_version": mc, "detected_by": "libraries/"}
    if fabric_loader and (intermediary or mc_server):
        return {"loader": "fabric", "loader_version": fabric_loader,
                "game_version": intermediary or mc_server,
                "detected_by": "libraries/"}
    return found


def _mod_jar_hint(data: bytes) -> tuple:
    """(loader, Minecraft-Version) aus einer Mod-.jar (nur Metadaten)."""
    import tomllib
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as jar:
            names = set(jar.namelist())
            if "fabric.mod.json" in names or "quilt.mod.json" in names:
                kind = "fabric" if "fabric.mod.json" in names else "quilt"
                obj = json.loads(jar.read(f"{kind}.mod.json").decode("utf-8", "replace"),
                                 strict=False)
                if kind == "fabric":
                    dep = (obj.get("depends") or {}).get("minecraft")
                else:
                    dep = next((d.get("versions") for d in
                                ((obj.get("quilt_loader") or {}).get("depends") or [])
                                if isinstance(d, dict) and d.get("id") == "minecraft"), None)
                dep = " ".join(dep) if isinstance(dep, list) else str(dep or "")
                m = _MC_VERSION_RE.search(dep)
                return kind, m.group(0) if m else None
            for meta, kind in (("META-INF/neoforge.mods.toml", "neoforge"),
                               ("META-INF/mods.toml", "forge")):
                if meta not in names:
                    continue
                obj = tomllib.loads(jar.read(meta).decode("utf-8", "replace"))
                mc = None
                for entries in (obj.get("dependencies") or {}).values():
                    for dep in entries if isinstance(entries, list) else []:
                        if isinstance(dep, dict) and dep.get("modId") == "minecraft":
                            m = _MC_VERSION_RE.search(str(dep.get("versionRange") or ""))
                            mc = mc or (m.group(0) if m else None)
                return kind, mc
    except (zipfile.BadZipFile, ValueError, KeyError, TypeError, AttributeError,
            OSError, tomllib.TOMLDecodeError):
        pass
    return None, None


def _server_pack_from_mods(archive: zipfile.ZipFile, jars: list) -> dict:
    """Fallback: Loader/MC-Version per Mehrheit aus den Mod-Metadaten."""
    from collections import Counter
    loaders: Counter[str] = Counter()
    versions: Counter[str] = Counter()
    for info in jars[:_SERVER_PACK_SCAN_JARS]:
        if info.file_size > _SERVER_PACK_MAX_JAR:
            continue
        with archive.open(info) as fh:
            loader, mc = _mod_jar_hint(fh.read())
        if loader:
            loaders[loader] += 1
        if mc:
            versions[mc] += 1
    if not loaders:
        return {}
    return {"loader": loaders.most_common(1)[0][0], "loader_version": None,
            "game_version": versions.most_common(1)[0][0] if versions else None,
            "detected_by": "Mod-Metadaten"}


def _detect_server_pack(archive: zipfile.ZipFile) -> dict | None:
    """Erkennt Server Files (Zip mit mods/-Ordner, ohne manifest.json).
    Rückgabe: Rohinfo für _normalize_index("serverpack", …) oder None."""
    names = archive.namelist()
    prefix = _server_pack_prefix(names)
    if prefix is None:
        return None
    jars = [i for i in archive.infolist()
            if not i.is_dir() and i.filename.startswith(prefix + "mods/")
            and i.filename.lower().endswith(".jar")
            and i.filename.count("/") == prefix.count("/") + 1]
    info = _server_pack_from_files(archive, prefix, names)
    if not info.get("loader") or not info.get("game_version"):
        fallback = _server_pack_from_mods(archive, jars)
        info = {**fallback, **{k: v for k, v in info.items() if v}}
    return {"prefix": prefix, "mod_count": len(jars),
            "title": prefix.rstrip("/"), **info}


def _extract_server_pack(pack_path: Path, root: Path, prefix: str,
                         progress=None) -> tuple:
    """Entpackt die serverrelevanten Ordner der Server Files nach root.
    Startskripte, Installer, libraries/ usw. braucht der Container nicht
    (itzg installiert die Server-Software selbst). Rückgabe: (dateien, mods,
    skipped[]); progress(bytes) wird je Datei aufgerufen."""
    allowed = set(_ALLOWED_TOP_DIRS) | set(_SERVER_PACK_EXTRA_DIRS)
    extracted, mods, skipped_tops = 0, 0, []
    with zipfile.ZipFile(pack_path) as archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.startswith(prefix):
                continue
            rel = info.filename[len(prefix):]
            if not rel:
                continue
            top = rel.split("/", 1)[0]
            if top.lower() not in allowed:
                if top not in skipped_tops:
                    skipped_tops.append(top)
                continue
            try:
                dest = _safe_pack_path(root, rel)
            except HTTPException:
                skipped_tops.append(f"{rel} (unsicherer Pfad)")
                continue
            if "__skipped__" in dest.parts:
                dest = root / rel  # Zusatzordner (scripts/, global_packs/ …)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(dest, "wb") as fh:
                shutil.copyfileobj(src, fh)
            extracted += 1
            if top.lower() == "mods" and rel.lower().endswith(".jar"):
                mods += 1
            if progress:
                progress(info.file_size)
    skipped = [f"{t} (vom Server nicht benötigt)" for t in skipped_tops]
    return extracted, mods, skipped


def _server_pack_bytes(pack_path: Path, prefix: str) -> int:
    allowed = set(_ALLOWED_TOP_DIRS) | set(_SERVER_PACK_EXTRA_DIRS)
    with zipfile.ZipFile(pack_path) as archive:
        return sum(i.file_size for i in archive.infolist()
                   if i.filename.startswith(prefix)
                   and i.filename[len(prefix):].split("/", 1)[0].lower() in allowed)


def _normalize_index(fmt: str, index: dict) -> dict:
    """Vereinheitlicht mrpack- und CurseForge-Index auf ein internes Schema:
    {title, game_version, loader, loader_version,
     files: [{path, downloads, fileSize, sha1}]}"""
    if not isinstance(index, dict):
        raise HTTPException(status_code=400, detail="Modpack-Index ist beschädigt")
    if fmt == "serverpack":
        return {
            "title": index.get("title") or "",
            "game_version": index.get("game_version"),
            "loader": index.get("loader"),
            "loader_version": index.get("loader_version"),
            "files": [],
            "server_pack": True,
            "prefix": index.get("prefix") or "",
            "mod_count": int(index.get("mod_count") or 0),
            "detected_by": index.get("detected_by"),
        }
    if fmt == "modrinth":
        deps = index.get("dependencies") or {}
        loader_map = {"fabric-loader": "fabric", "forge": "forge",
                      "neoforge": "neoforge", "quilt-loader": "quilt"}
        loader, loader_version = None, None
        for key, value in deps.items():
            if key in loader_map:
                loader, loader_version = loader_map[key], str(value or "")
                break
        return {
            "title": (index.get("name") or "").strip(),
            # Neues mrpack-Format: gameVersion leer, MC-Version in deps.minecraft
            "game_version": index.get("gameVersion") or deps.get("minecraft"),
            "loader": loader,
            "loader_version": loader_version,
            "files": index.get("files") or [],
        }
    # CurseForge: minecraft.version + modLoaders[].id (z. B. "fabric-0.16.9")
    mc = index.get("minecraft") or {}
    loaders = [ld for ld in (mc.get("modLoaders") or []) if isinstance(ld, dict)]
    primary = next((ld for ld in loaders if ld.get("primary")), loaders[0] if loaders else None)
    cf_id = str((primary or {}).get("id") or "")
    loader, loader_version = None, None
    for prefix, name in (("fabric-", "fabric"), ("forge-", "forge"),
                         ("neoforge-", "neoforge"), ("quilt-", "quilt")):
        if cf_id.startswith(prefix):
            loader, loader_version = name, cf_id[len(prefix):]
            break
    files = []
    for f in index.get("files") or []:
        if not isinstance(f, dict):
            continue
        pid, fid = f.get("projectID"), f.get("fileID")
        if not isinstance(pid, int) or not isinstance(fid, int) or pid <= 0 or fid <= 0:
            continue
        files.append({
            "path": None,  # Dateiname ergibt sich beim Download (CDN-Redirect)
            "downloads": [_CF_DL_URL.format(pid=pid, fid=fid)],
            "fileSize": 0,
            "sha1": None,
        })
    return {
        "title": (index.get("name") or "").strip(),
        "game_version": mc.get("version"),
        "loader": loader,
        "loader_version": loader_version,
        "files": files,
    }


def _file_plan(root: Path, index: dict) -> tuple:
    """Erstellt den Download-Plan: [(url, ziel, größe, sha1)], skipped[], failed_paths.
    Dateien, die das Pack als serverseitig 'unsupported' markiert (nur-Client-
    Mods), werden für die Server-Instanz übersprungen."""
    plan, skipped = [], []
    for entry in index.get("files") or []:
        rel = entry.get("path") or ""
        if not rel:
            skipped.append("(ohne Pfad)")
            continue
        try:
            dest = _safe_pack_path(root, rel)
        except HTTPException:
            skipped.append(f"{rel} (unsicherer Pfad)")
            continue
        if "__skipped__" in dest.parts:
            skipped.append(f"{rel} (Ordner nicht erlaubt)")
            continue
        env = entry.get("env") or {}
        if str(env.get("server") or "required").lower() == "unsupported":
            skipped.append(f"{rel} (nur Client — für Server übersprungen)")
            continue
        urls = [u for u in (entry.get("downloads") or []) if str(u).startswith("https://")]
        if not urls:
            skipped.append(f"{rel} (kein HTTPS-Download)")
            continue
        sha1 = (entry.get("hashes") or {}).get("sha1")
        plan.append((urls[0], dest, int(entry.get("fileSize") or 0), sha1))
    return plan, skipped


async def _download_one(client: httpx.AsyncClient, url: str, dest: Path,
                        expected_size: int, sha1) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    digest = hashlib.sha1() if sha1 else None
    try:
        async with client.stream("GET", url, headers=_headers()) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code} bei {url}")
            with open(part, "wb") as fh:
                async for chunk in resp.aiter_bytes(65536):
                    fh.write(chunk)
                    if digest:
                        digest.update(chunk)
        if expected_size and abs(part.stat().st_size - expected_size) > 65536:
            raise RuntimeError(f"Unvollständiger Download ({part.stat().st_size} Bytes)")
        if (digest and part.stat().st_size < 64 * 1024 * 1024
                and digest.hexdigest() != sha1):  # große Dateien überspringen Hash
            raise RuntimeError("SHA1-Prüfung fehlgeschlagen")
        os.replace(part, dest)
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass


class _CfTransientError(Exception):
    """Transienter Fehler beim CurseForge-Download (403/429/5xx, Netzwerk)."""


async def _download_cf_one(client: httpx.AsyncClient, url: str, mods_root: Path,
                           sha1=None, verify_url: bool = False):
    """Lädt eine CurseForge-Datei; der Dateiname ergibt sich aus der finalen
    CDN-URL nach Redirect. Rückgabe: Dateiname oder None (kein .jar).
    Mit sha1 wird die Integrität geprüft; verify_url begrenzt den Ziel-Host
    auf die CurseForge-CDN-Allowlist (Anti-SSRF). Transiente Fehler werden
    bis zu _CF_RETRIES-mal mit steigender Pause wiederholt."""
    attempt = 0
    while True:
        try:
            return await _download_cf_once(client, url, mods_root,
                                           sha1=sha1, verify_url=verify_url)
        except _CfTransientError as exc:
            if attempt >= _CF_RETRIES:
                raise RuntimeError(f"{exc} (nach {attempt + 1} Versuchen)") from exc
            attempt += 1
            await asyncio.sleep(_CF_RETRY_DELAY * attempt)


def _is_client_only_mod(filename: str) -> bool:
    """Heuristik: bekannte Client-only-Mods ( siehe _CLIENT_ONLY_PATTERNS)
    plus optionale Zusatzmuster aus CF_CLIENT_ONLY_MODS (env)."""
    lower = filename.lower()
    return (any(p in lower for p in _CLIENT_ONLY_PATTERNS)
            or any(p in lower for p in settings.cf_client_only_mods))


async def _download_cf_once(client: httpx.AsyncClient, url: str, mods_root: Path,
                            sha1=None, verify_url: bool = False):
    """Ein Download-Versuch einer CurseForge-Datei (siehe _download_cf_one)."""
    from urllib.parse import unquote

    from .security import validate_filename
    part = None
    try:
        try:
            async with client.stream("GET", url, headers=_headers()) as resp:
                if resp.status_code != 200:
                    msg = f"HTTP {resp.status_code} bei CurseForge-Download"
                    if resp.status_code in _CF_RETRY_STATUS:
                        raise _CfTransientError(msg)
                    raise RuntimeError(msg)
                if verify_url:
                    validate_curseforge_download_url(str(resp.url))
                name = unquote(str(resp.url.path).rstrip("/").split("/")[-1] or "")
                if not name.lower().endswith(".jar"):
                    return None  # Ressourcenpakete etc. gehören nicht nach mods/
                if _is_client_only_mod(name):
                    return None  # bekannter Client-only-Crasher → überspringen
                validate_filename(name)
                dest = mods_root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                part = dest.with_suffix(dest.suffix + ".part")
                digest = hashlib.sha1() if sha1 else None
                with open(part, "wb") as fh:
                    async for chunk in resp.aiter_bytes(65536):
                        fh.write(chunk)
                        if digest:
                            digest.update(chunk)
        except httpx.TransportError as exc:
            raise _CfTransientError(
                f"Netzwerkfehler bei CurseForge-Download: "
                f"{exc or exc.__class__.__name__}") from exc
        if (digest and part.stat().st_size < 64 * 1024 * 1024
                and digest.hexdigest() != sha1):
            raise RuntimeError("SHA1-Prüfung fehlgeschlagen")
        os.replace(part, dest)
        return name
    finally:
        if part is not None:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass


def _extract_overrides(pack_path: Path, root: Path) -> tuple:
    """Extrahiert den overrides/-Ordner eines CurseForge-Packs (mit Pfad-Schutz).
    Rückgabe: (anzahl, skipped[])."""
    extracted, skipped = 0, []
    with zipfile.ZipFile(pack_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if not name.startswith("overrides/"):
                continue
            rel = name[len("overrides/"):]
            if not rel:
                continue
            try:
                dest = _safe_pack_path(root, rel)
            except HTTPException:
                skipped.append(f"{rel} (unsicherer Pfad)")
                continue
            if "__skipped__" in dest.parts:
                skipped.append(f"{rel} (Ordner nicht erlaubt)")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(info) as src, open(dest, "wb") as fh:
                    shutil.copyfileobj(src, fh)
            except OSError as exc:
                skipped.append(f"{rel} (nicht schreibbar: {exc.__class__.__name__})")
                continue
            extracted += 1
    return extracted, skipped


async def install_pack(instance_id: str, project_id: str,
                       version_id: str = None, force: bool = False) -> dict:
    """Startet einen Modpack-Installations-Job (asynchron, Fortschritt im Job)."""
    _validate_id(project_id, "Projekt-ID")
    if version_id:
        _validate_id(version_id, "Versions-ID")
    instance = get_instance(instance_id)

    active = _running_pack_job(instance_id)
    if active:
        raise HTTPException(status_code=409,
                            detail=f"Es läuft bereits eine Installation "
                                   f"({active.get('filename')}) für diese Instanz")
    existing = instance.get("modpack")
    if existing and not force:
        _conflict(existing)

    async with _new_client() as client:
        version = await _resolve_pack_version(client, instance, project_id, version_id)
        filename, url, size = _pick_pack_file(version)
        if size > _MAX_PACK_BYTES:
            raise HTTPException(status_code=413,
                                detail=f"Modpack zu groß ({size // (1024**2)} MiB, max 4 GiB)")
    # Vorab-Speicherplatz-Prüfung (mrpack + geschätzte Mods = 3x Puffer)
    _check_disk_space(instance_dir(instance_id), size * 3)

    from .instances import validate_pack_filename
    validate_pack_filename(filename)
    dest = pack_dir(instance_id) / filename
    await _safety_snapshot(instance_id)
    job = modrinth.create_job(filename, size, kind="pack", phase="Modpack-Download",
                              instance_id=instance_id, project_id=project_id,
                              version_id=version.get("id"))
    modrinth.track_task(asyncio.create_task(
        _run_install(job, instance, url, dest, version)))
    return job


async def install_pack_cf(instance_id: str, project_id: str,
                          file_id: str = None, force: bool = False) -> dict:
    """Startet die Installation eines CurseForge-Modpacks über die CF-API:
    Pack-Zip vom CDN laden → manifest prüfen → Overrides + Mods installieren."""
    from . import curseforge  # lazy, vermeidet Import-Zirkel
    curseforge._require_key()
    _validate_id(project_id, "Projekt-ID")
    instance = get_instance(instance_id)

    active = _running_pack_job(instance_id)
    if active:
        raise HTTPException(status_code=409,
                            detail=f"Es läuft bereits eine Installation "
                                   f"({active.get('filename')}) für diese Instanz")
    existing = instance.get("modpack")
    if existing and not force:
        _conflict(existing)

    mod, filename, url, size, sha1, chosen_file_id = await curseforge.resolve_pack(
        project_id, file_id)
    if size > _MAX_PACK_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"Modpack zu groß ({size // (1024**2)} MiB, max 4 GiB)")
    # Zip + geschätzte Mods = 3x Puffer
    _check_disk_space(instance_dir(instance_id), size * 3)

    from .instances import validate_pack_filename
    validate_pack_filename(filename)
    dest = pack_dir(instance_id) / filename
    await _safety_snapshot(instance_id)
    job = modrinth.create_job(
        filename, size, kind="pack", phase="Modpack-Download",
        instance_id=instance_id,
        project_id=str(mod.get("slug") or mod.get("id") or project_id),
        version_id=str(chosen_file_id) if chosen_file_id else None,
        source="curseforge")
    modrinth.track_task(asyncio.create_task(
        _run_cf_pack_install(job, instance, url, dest, sha1)))
    return job


async def _run_cf_pack_install(job: dict, instance: dict, url: str, dest: Path,
                               sha1=None) -> None:
    """Hintergrund-Job: CurseForge-Pack-Zip laden → installieren (Upload-Pipeline)."""
    try:
        await _download_archive(job, url, dest, sha1=sha1, verify_url=True)
        job["phase"] = "Prüfung (Kompatibilität)"
        await _run_upload_install(job, instance, dest, source="curseforge")
    except HTTPException as exc:
        job["status"] = "error"
        job["error"] = str(exc.detail)
        job["phase"] = "Fehlgeschlagen"
        modrinth.persist_job(job)
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc) or exc.__class__.__name__
        job["phase"] = "Fehlgeschlagen"
        modrinth.persist_job(job)
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass


def _conflict(existing: dict):
    raise HTTPException(
        status_code=409,
        detail=(f"Es ist bereits das Modpack '{existing.get('title')}' installiert — "
                f"Überschreiben mit force=true bestätigen"))


async def _safety_snapshot(instance_id: str) -> None:
    """Sicherheits-Snapshot der Instanz, bevor eine Installation bestehende
    mods-/config-Dateien überschreiben kann (macht force risikofrei). Ohne
    Mods in der Instanz gibt es nichts zu sichern. Ein fehlgeschlagener
    Snapshot bricht die Installation ab (kein stiller Datenverlust)."""
    from . import backups  # lazy, vermeidet Import-Zirkel
    from .instances import mods_dir  # lazy, vermeidet Import-Zirkel
    mods = mods_dir(instance_id)
    try:
        has_mods = any(mods.iterdir())
    except OSError:
        has_mods = False
    if not has_mods:
        return
    await asyncio.to_thread(
        backups.safety_backup, instance_id, instance_dir(instance_id),
        "pre-install")


async def _run_install(job: dict, instance: dict, url: str, dest: Path, version: dict) -> None:
    """Hintergrund-Job: mrpack laden → prüfen → Dateien laden → Metadaten setzen."""
    root = instance_dir(instance["id"])
    try:
        # 1) mrpack herunterladen
        await _download_archive(job, url, dest)
        job["phase"] = "Prüfung (Kompatibilität)"

        # 2) Index lesen, normalisieren (mrpack/curseforge → internes Schema)
        fmt, raw_index = _extract_index(dest)
        index = _normalize_index(fmt, raw_index)
        loader_dep = _check_compatibility(instance, index)

        # 3) Speicherplatz
        plan, skipped = _file_plan(root, index)
        total_mod_bytes = sum(size for _, _, size, _ in plan)
        job["phase"] = "Prüfung (Speicherplatz)"
        _check_disk_space_job(root, total_mod_bytes)

        # 4) Dateien parallel laden
        job["status"] = "installing"
        job["total"] = job["downloaded"] + total_mod_bytes
        job["phase"] = f"Mods installieren (0/{len(plan)})"
        sem = asyncio.Semaphore(_CONCURRENCY)
        failures: list[str] = []
        completed = {"count": 0}

        async def worker(item):
            u, d, s, h = item
            async with sem:
                try:
                    await _download_one(dl_client, u, d, s, h)
                    job["downloaded"] += s
                except Exception as exc:
                    failures.append(f"{d.name}: {exc}")
                finally:
                    completed["count"] += 1
                    job["phase"] = (f"Mods installieren "
                                    f"({completed['count']}/{len(plan)})")

        async with _new_client() as dl_client:
            await asyncio.gather(*(worker(item) for item in plan))
        if failures:
            shown = "; ".join(failures[:5])
            more = (f" … +{len(failures) - 5} weitere"
                    if len(failures) > 5 else "")
            raise RuntimeError(f"{len(failures)} Mod-Datei(en) fehlgeschlagen: "
                               f"{shown}{more}")

        # 5) Instanz-Metadaten aktualisieren
        instance = get_instance(instance["id"])
        instance["modpack"] = {
            "project_id": job.get("project_id"),
            "version_id": version.get("id"),
            "title": (version.get("name") or job.get("filename") or "").strip(),
            "game_version": loader_dep.get("game_version"),
            "loader": loader_dep["loader"],
            "loader_version": loader_dep["version"] or instance.get("loader_version"),
            "files": len(plan),
            "skipped": skipped,
        }
        if loader_dep["version"]:
            instance["loader_version"] = loader_dep["version"]
        update_instance(instance)
        job["status"] = "done"
        job["phase"] = "Fertig"
        job["summary"] = {"files": len(plan), "skipped": skipped}
    except HTTPException as exc:
        job["status"] = "error"
        job["error"] = str(exc.detail)
        job["phase"] = "Fehlgeschlagen"
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc) or exc.__class__.__name__
        job["phase"] = "Fehlgeschlagen"
    finally:
        if job["status"] != "done":
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
        modrinth.persist_job(job)


async def install_upload(instance_id: str, archive_path: Path, filename: str,
                         force: bool = False, auto_version: bool = True) -> dict:
    """Startet die Installation eines hochgeladenen Modpack-Archivs
    (.mrpack = Modrinth, .zip = CurseForge-Export) als Hintergrund-Job.

    auto_version=True (Standard): Benötigte MC-Version/Loader werden aus dem
    Archiv gelesen und die Instanz bei Abweichung automatisch umgestellt,
    statt mit 'Inkompatibel' abzubrechen."""
    instance = get_instance(instance_id)
    active = _running_pack_job(instance_id)
    if active:
        raise HTTPException(status_code=409,
                            detail=f"Es läuft bereits eine Installation "
                                   f"({active.get('filename')}) für diese Instanz")
    existing = instance.get("modpack")
    if existing and not force:
        _conflict(existing)
    try:
        size = archive_path.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f"Hochgeladenes Archiv nicht lesbar: {exc}")
    if size > _MAX_PACK_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"Modpack zu groß ({size // (1024**2)} MiB, max 4 GiB)")
    _check_disk_space(instance_dir(instance_id), size * 3)

    await _safety_snapshot(instance_id)
    job = modrinth.create_job(filename, size, kind="pack", phase="Archiv prüfen",
                              instance_id=instance_id, source="upload")
    modrinth.track_task(asyncio.create_task(
        _run_upload_install(job, instance, archive_path,
                            auto_version=auto_version)))
    return job


def _adapt_phase_note(adapted: dict) -> str:
    """Kurzer Job-Phasen-Text für eine vorgenommene Versions-Umstellung."""
    a = adapted["to"]
    return f"Instanz umgestellt auf Minecraft {a['game_version']} ({a['loader']})"


def _failure_summary(failures: list) -> str:
    shown = "; ".join(failures[:5])
    more = f" … +{len(failures) - 5} weitere" if len(failures) > 5 else ""
    return f"{len(failures)} Mod-Datei(en) fehlgeschlagen: {shown}{more}"


async def _download_entries(job: dict, root: Path, entries: list) -> tuple:
    """Lädt Pack-Dateien parallel. entries: [{kind: "mr"|"cf", url, path,
    size, sha1, label}]. Rückgabe: (installiert, skipped[], fehler[], offen[])
    — offen = Einträge, die fehlgeschlagen sind (für „erneut versuchen“)."""
    mods_root = root / "mods"
    sem = asyncio.Semaphore(_CONCURRENCY)
    installed = {"n": 0, "done": 0}
    skipped: list[str] = []
    failures: list[str] = []
    missing: list[dict] = []
    total = len(entries)

    async def worker(entry):
        async with sem:
            try:
                if entry["kind"] == "mr":
                    await _download_one(dl_client, entry["url"],
                                        root / entry["path"],
                                        int(entry.get("size") or 0), entry.get("sha1"))
                    job["downloaded"] += int(entry.get("size") or 0)
                    installed["n"] += 1
                else:
                    name = await _download_cf_one(dl_client, entry["url"], mods_root)
                    if name is None:
                        skipped.append(f"CurseForge-Datei {entry['label']} "
                                       f"(kein .jar oder bekannte Client-only-Mod)")
                    else:
                        installed["n"] += 1
                        try:
                            job["downloaded"] += (mods_root / name).stat().st_size
                        except OSError:
                            pass
            except Exception as exc:
                failures.append(f"{entry['label']}: {exc}")
                missing.append(entry)
            finally:
                installed["done"] += 1
                job["phase"] = f"Mods installieren ({installed['done']}/{total})"

    async with _new_client() as dl_client:
        await asyncio.gather(*(worker(e) for e in entries))
    return installed["n"], skipped, failures, missing


def _plan_entries(fmt: str, root: Path, pack: dict) -> tuple:
    """Download-Einträge für _download_entries + übersprungene Dateien."""
    if fmt == "modrinth":
        plan, skipped = _file_plan(root, pack)
        entries = [{"kind": "mr", "url": u, "path": str(d.relative_to(root)),
                    "size": s, "sha1": h, "label": d.name} for u, d, s, h in plan]
        return entries, skipped
    entries = []
    for f in pack["files"]:
        url = (f.get("downloads") or [""])[0]
        entries.append({"kind": "cf", "url": url,
                        "label": url.rsplit("/", 1)[-1] if url else "?"})
    return entries, []


async def _run_upload_install(job: dict, instance: dict, dest: Path,
                              source: str = "upload",
                              auto_version: bool = False) -> None:
    """Hintergrund-Job für Uploads: Index lesen → prüfen → Dateien installieren.

    auto_version=True: Bei abweichender MC-Version/Loader-Kombination wird die
    Instanz automatisch umgestellt (nur Upload-Pfad; CF-Suche bleibt streng).

    Einzelne fehlgeschlagene Downloads brechen die Installation nicht ab:
    Der Rest wird installiert, die fehlenden Dateien landen in
    modpack["failed"] und lassen sich mit retry_failed nachladen.
    """
    root = instance_dir(instance["id"])
    failures: list[str] = []
    try:
        job["phase"] = "Prüfung (Kompatibilität)"
        # Index lesen ist billig (zentrales Verzeichnis + JSON/variables.txt);
        # nur bei Server Files ohne Begleitdateien werden bis zu
        # _SERVER_PACK_SCAN_JARS Mod-Metadaten gelesen.
        fmt, raw_index = _extract_index(dest)
        pack = _normalize_index(fmt, raw_index)
        adapted = None
        if auto_version and _needs_version_adaptation(instance, pack):
            loader_dep = _adapt_instance_for_pack(instance, pack)
            adapted = {"from": loader_dep["from"], "to": loader_dep["to"]}
            job["phase"] = _adapt_phase_note(adapted)
        else:
            loader_dep = _check_compatibility(instance, pack)

        skipped, entries, missing, installed = [], [], [], 0
        if fmt == "serverpack":
            job["phase"] = "Prüfung (Speicherplatz)"
            unpacked = await asyncio.to_thread(_server_pack_bytes, dest, pack["prefix"])
            _check_disk_space_job(root, unpacked)
            job["status"] = "installing"
            job["downloaded"], job["total"] = 0, unpacked
            job["phase"] = "Server Files entpacken"

            def _progress(nbytes: int) -> None:
                job["downloaded"] += nbytes

            _, installed, over_skip = await asyncio.to_thread(
                _extract_server_pack, dest, root, pack["prefix"], _progress)
            skipped.extend(over_skip)
        else:
            entries, skipped = _plan_entries(fmt, root, pack)
            if fmt == "curseforge":
                job["phase"] = "Overrides extrahieren"
                _, over_skip = await asyncio.to_thread(_extract_overrides, dest, root)
                skipped.extend(over_skip)
            total_bytes = sum(int(e.get("size") or 0) for e in entries)
            job["phase"] = "Prüfung (Speicherplatz)"
            _check_disk_space_job(root, total_bytes)
            job["status"] = "installing"
            # Modrinth: Archiv-Größe (Upload) + bekannte Mod-Größe ·
            # CurseForge: Mod-Größen unbekannt → nur Phase zeigen
            job["total"] = job["downloaded"] + total_bytes if fmt == "modrinth" else 0
            job["phase"] = f"Mods installieren (0/{len(entries)})"
            installed, dl_skipped, failures, missing = await _download_entries(
                job, root, entries)
            skipped.extend(dl_skipped)
            if failures and installed == 0:
                raise RuntimeError(_failure_summary(failures))

        # Instanz-Metadaten aktualisieren
        instance = get_instance(instance["id"])
        instance["modpack"] = {
            "project_id": job.get("project_id"),
            "version_id": job.get("version_id"),
            "title": pack.get("title") or job.get("filename") or "Hochgeladenes Modpack",
            "source": source,
            "format": fmt,
            "game_version": loader_dep["game_version"],
            "loader": loader_dep["loader"],
            "loader_version": loader_dep["version"] or instance.get("loader_version"),
            "files": installed,
            "skipped": skipped,
        }
        if missing:
            instance["modpack"]["failed"] = missing
        if adapted:
            instance["modpack"]["version_adapted"] = adapted
        if loader_dep["version"]:
            instance["loader_version"] = loader_dep["version"]
        update_instance(instance)
        job["status"] = "done"
        job["phase"] = ("Fertig — einige Mods fehlen" if missing else "Fertig")
        job["summary"] = {"files": installed, "skipped": skipped,
                          "format": fmt}
        if failures:
            job["failed"] = failures
            job["summary"]["failed"] = failures
        if adapted:
            job["summary"]["version_adapted"] = adapted
    except HTTPException as exc:
        job["status"] = "error"
        job["error"] = str(exc.detail)
        job["phase"] = "Fehlgeschlagen"
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc) or exc.__class__.__name__
        if failures:
            job["failed"] = failures  # vollständige Liste für die UI
        job["phase"] = "Fehlgeschlagen"
    finally:
        if job["status"] != "done":
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
        modrinth.persist_job(job)


def _valid_failed_entry(root: Path, entry) -> dict | None:
    """Prüft einen gespeicherten Fehl-Eintrag erneut (stammt aus instance.json)."""
    if not isinstance(entry, dict) or entry.get("kind") not in ("mr", "cf"):
        return None
    url = str(entry.get("url") or "")
    if not url.startswith("https://"):
        return None
    if entry["kind"] == "cf":
        if not url.startswith(_CF_DL_URL.split("{", 1)[0]):
            return None
        return {"kind": "cf", "url": url, "label": str(entry.get("label") or "?")}
    try:
        dest = _safe_pack_path(root, str(entry.get("path") or ""))
    except HTTPException:
        return None
    if "__skipped__" in dest.parts:
        return None
    return {"kind": "mr", "url": url, "path": str(dest.relative_to(root)),
            "size": int(entry.get("size") or 0), "sha1": entry.get("sha1"),
            "label": dest.name}


async def retry_failed(instance_id: str) -> dict:
    """Lädt nur die bei der letzten Pack-Installation fehlgeschlagenen
    Dateien erneut (Hintergrund-Job)."""
    instance = get_instance(instance_id)
    active = _running_pack_job(instance_id)
    if active:
        raise HTTPException(status_code=409,
                            detail=f"Es läuft bereits eine Installation "
                                   f"({active.get('filename')}) für diese Instanz")
    root = instance_dir(instance_id)
    mp = instance.get("modpack") or {}
    entries = [e for e in (_valid_failed_entry(root, x) for x in mp.get("failed") or [])
               if e]
    if not entries:
        raise HTTPException(status_code=400,
                            detail="Keine fehlgeschlagenen Modpack-Dateien vorhanden")
    job = modrinth.create_job(mp.get("title") or "Modpack", 0, kind="pack",
                              phase=f"Mods installieren (0/{len(entries)})",
                              instance_id=instance_id, source="retry")
    job["status"] = "installing"

    async def run():
        try:
            installed, skipped, failures, missing = await _download_entries(
                job, root, entries)
            inst = get_instance(instance_id)
            pack_meta = inst.get("modpack") or {}
            pack_meta["files"] = int(pack_meta.get("files") or 0) + installed
            if missing:
                pack_meta["failed"] = missing
            else:
                pack_meta.pop("failed", None)
            inst["modpack"] = pack_meta
            update_instance(inst)
            job["status"] = "done"
            job["phase"] = "Fertig — einige Mods fehlen" if missing else "Fertig"
            job["summary"] = {"files": installed, "skipped": skipped}
            if failures:
                job["failed"] = failures
                job["summary"]["failed"] = failures
        except Exception as exc:
            job["status"] = "error"
            job["error"] = str(exc) or exc.__class__.__name__
            job["phase"] = "Fehlgeschlagen"
        finally:
            modrinth.persist_job(job)

    modrinth.track_task(asyncio.create_task(run()))
    return job


async def _download_archive(job: dict, url: str, dest: Path,
                            sha1=None, verify_url: bool = False) -> None:
    """Lädt das .mrpack-/Pack-Archiv mit Fortschritt im Job; optional SHA1-Prüfung
    und CurseForge-CDN-Host-Allowlist (Anti-SSRF)."""
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        async with _new_client() as client, client.stream("GET", url, headers=_headers()) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"Modpack-Download: HTTP {resp.status_code}")
            if verify_url:
                validate_curseforge_download_url(str(resp.url))
            digest = hashlib.sha1() if sha1 else None
            with open(part, "wb") as fh:
                async for chunk in resp.aiter_bytes(65536):
                    fh.write(chunk)
                    job["downloaded"] += len(chunk)
                    if digest:
                        digest.update(chunk)
        if job["total"] and abs(job["downloaded"] - job["total"]) > 65536:
            raise RuntimeError("Modpack-Download unvollständig")
        if (digest and part.stat().st_size < 64 * 1024 * 1024
                and digest.hexdigest() != sha1):
            raise RuntimeError("SHA1-Prüfung fehlgeschlagen")
        os.replace(part, dest)
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Server direkt aus Modpack erstellen (ohne bestehende Instanz)
# ---------------------------------------------------------------------------

def _pick_modrinth_loader(version: dict):
    """Unterstützten Loader einer mrpack-Version wählen."""
    loaders = [str(x).strip().lower() for x in (version.get("loaders") or [])]
    for candidate in _LOADER_PREFERENCE:
        if candidate in loaders:
            return candidate
    return None


def _pick_modrinth_game_version(version: dict):
    """Minecraft-Version einer mrpack-Version wählen (neueste zuerst)."""
    for v in (version.get("game_versions") or []):
        v = str(v).strip()
        if _MODRINTH_GAME_VERSION_RE.match(v):
            return v
    return None


def _sanitize_name(title: str) -> str:
    """Pack-Titel in einen gültigen Instanznamen überführen."""
    cleaned = _NAME_CLEAN_RE.sub(" ", (title or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(" .-")
    if len(cleaned) > 64:
        cleaned = cleaned[:64].rstrip(" .- ")  # noqa: B005 (Zeichen-Set)
    if not cleaned:
        return "Modpack-Server"
    if not cleaned[0].isalnum():
        cleaned = f"Server {cleaned}"
    return cleaned


def _unique_name(base: str) -> str:
    """Freien Instanznamen ableiten (Anhängen von ' 2', ' 3', …)."""
    existing = {i["name"].lower() for i in list_instances()}
    name = base
    suffix = 2
    while name.lower() in existing:
        if suffix > 50:
            raise HTTPException(status_code=409,
                                detail="Kann keinen freien Instanznamen ableiten")
        tail = f" {suffix}"
        name = base[:64 - len(tail)].rstrip(" .-") + tail
        suffix += 1
    return name


async def _resolve_pack_version_global(client: httpx.AsyncClient, project_id: str,
                                       version_id):
    """Modpack-Version ohne Instanzbezug auflösen (neueste = erste)."""
    if version_id:
        version = await modrinth._get_json(client, f"/version/{version_id}")
        if version.get("project_id") != project_id:
            raise HTTPException(status_code=400,
                                detail="Versions-ID gehört nicht zum Projekt")
        return version
    versions = await modrinth._get_json(client, f"/project/{project_id}/version")
    if not versions:
        raise HTTPException(status_code=404, detail="Modpack-Versionen nicht gefunden")
    return versions[0]


def _check_pack_size(size: int) -> None:
    if size > _MAX_PACK_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"Modpack zu groß ({size // (1024**2)} MiB, max 4 GiB)")


async def create_server_from_pack(*, project_id: str, source: str = "modrinth",
                                  version_id: str = None, file_id: str = None,
                                  name: str = None, memory: str = None,
                                  port: int = None, accept_eula: bool = False,
                                  prefer_server_pack: bool = True) -> dict:
    """Erstellt direkt aus einem Modpack eine neue Server-Instanz:
    MC-Version und Loader stammen aus den Pack-Metadaten, danach läuft die
    normale Install-Pipeline in die neue Instanz.

    CurseForge: wird eine separate Server-Pack-Datei (serverPackFileId)
    angeboten, wird diese bevorzugt installiert — sie enthält nur die
    serverrelevanten Dateien statt der kompletten Client-Packung.
    """
    _validate_id(project_id, "Projekt-ID")
    install_version_id, install_file_id, size = None, None, 0
    if source == "curseforge":
        from . import curseforge  # lazy, vermeidet Import-Zirkel
        curseforge._require_key()
        mod, chosen, server_file = await curseforge.resolve_pack_file(
            project_id, file_id, server_pack=prefer_server_pack)
        # Metadaten primär aus der Haupt-Pack-Datei (reichere Tags)
        game_version, loader = curseforge.pack_meta_from_file(chosen)
        if not loader or not game_version:
            gv2, loader2 = curseforge.pack_meta_from_file(server_file or {})
            game_version = game_version or gv2
            loader = loader or loader2
        title = str(mod.get("name") or project_id)
        install_file_id = str((server_file or chosen).get("id"))
        size = int((server_file or chosen).get("fileSize") or 0)
    else:
        async with _new_client() as client:
            version = await _resolve_pack_version_global(client, project_id,
                                                         version_id)
            _, _, size = _pick_pack_file(version)
        loader = _pick_modrinth_loader(version)
        game_version = _pick_modrinth_game_version(version)
        title = str(version.get("name") or project_id)
        install_version_id = version.get("id")
    _check_pack_size(size)

    loader = (loader or "").strip().lower()
    if loader not in ALLOWED_LOADERS:
        raise HTTPException(
            status_code=400,
            detail=(f"Modpack-Loader '{loader or '?'}' kann für einen Server "
                    f"nicht automatisch bestimmt werden — bitte Server manuell "
                    f"erstellen (Tab „Server“)"))
    if not game_version:
        raise HTTPException(
            status_code=400,
            detail=("Modpack gibt keine Minecraft-Version an — bitte Server "
                    "manuell erstellen (Tab „Server“)"))
    game_version = validate_identifier(str(game_version).strip(), "Minecraft-Version")

    _check_disk_space(settings.instances_dir, size * 3)
    instance = create_instance(
        _unique_name(_sanitize_name(name or title)), loader, game_version,
        loader_version=None, port=port, memory=memory, accept_eula=accept_eula)
    try:
        if source == "curseforge":
            job = await install_pack_cf(instance["id"], project_id,
                                        file_id=install_file_id)
        else:
            job = await install_pack(instance["id"], project_id,
                                     version_id=install_version_id)
    except Exception:
        # Neue, leere Instanz wieder entfernen, damit kein Müll zurückbleibt
        shutil.rmtree(instance_dir(instance["id"]), ignore_errors=True)
        raise
    return {"instance": instance, "job": job}


def staging_dir() -> Path:
    """Zwischenablage für Uploads vor der Instanzerstellung (gleiches Volume)."""
    path = settings.instances_dir / "_staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _meta_from_archive(archive: Path) -> tuple:
    """(loader, game_version, title, mod_count) aus einem hochgeladenen Pack-Archiv."""
    fmt, raw_index = _extract_index(archive)
    pack = _normalize_index(fmt, raw_index)
    loader, game_version = pack.get("loader"), pack.get("game_version")
    if not loader or not game_version:
        raise HTTPException(
            status_code=400,
            detail=("Modpack deklariert Loader/Minecraft-Version nicht eindeutig — "
                    "bitte Server manuell erstellen (Tab „Server“)"))
    return loader, str(game_version), pack.get("title"), _mod_count(fmt, pack)


# ---------------------------------------------------------------------------
# Vorschau: Archiv vor der Installation analysieren
# ---------------------------------------------------------------------------

# (Mods unter Grenze → RAM); darüber _MEMORY_MAX. Werte passen zu den
# Auswahlfeldern im Dashboard.
_MEMORY_STEPS = ((60, "4G"), (150, "6G"), (250, "8G"))
_MEMORY_MAX = "12G"
_PREVIEW_PREFIX = "preview_"
_PREVIEW_MAX_AGE = 12 * 3600  # liegengebliebene Vorschau-Uploads aufräumen
_PREVIEW_TOKEN_RE = re.compile(r"^[0-9a-f]{16}$")


def recommend_memory(mod_count: int) -> str | None:
    """RAM-Empfehlung nach Mod-Anzahl (None = Standard reicht)."""
    if mod_count <= 0:
        return None
    for limit, memory in _MEMORY_STEPS:
        if mod_count < limit:
            return memory
    return _MEMORY_MAX


def _memory_bytes(value: str) -> int:
    value = str(value or "").strip().upper()
    if not value[:-1].isdigit():
        return 0
    return int(value[:-1]) * (1024 ** 3 if value.endswith("G") else 1024 ** 2)


def host_memory_bytes() -> int:
    """Gesamt-RAM des Hosts (bzw. Container-Limit, falls kleiner); 0 = unbekannt."""
    total = 0
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
                    break
    except (OSError, ValueError):
        pass
    try:
        raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if raw.isdigit() and (not total or int(raw) < total):
            total = int(raw)
    except OSError:
        pass
    return total


def _mod_count(fmt: str, pack: dict) -> int:
    if fmt == "serverpack":
        return int(pack.get("mod_count") or 0)
    if fmt == "curseforge":
        return len(pack.get("files") or [])
    return sum(1 for f in pack.get("files") or []
               if str(f.get("path") or "").replace("\\", "/").startswith("mods/"))


def _title_from_filename(filename: str) -> str:
    stem = re.sub(r"\.(zip|mrpack)$", "", filename or "", flags=re.I)
    return stem.replace("_", " ").strip()


def analyze_archive(archive: Path, filename: str) -> dict:
    """Liest ein Pack-Archiv (blockierend, im Thread aufrufen) und liefert
    alles, was die Vorschau zeigt: Format, Titel, Minecraft/Loader, Java,
    Anzahl Mods, RAM-Empfehlung und Warnungen."""
    from . import runtime  # lazy, vermeidet Import-Zirkel
    fmt, raw = _extract_index(archive)
    pack = _normalize_index(fmt, raw)
    loader = str(pack.get("loader") or "").strip().lower() or None
    game_version = str(pack.get("game_version") or "").strip() or None
    mods = _mod_count(fmt, pack)
    memory = recommend_memory(mods)
    warnings: list[str] = []
    errors: list[str] = []
    if not loader or not game_version:
        errors.append("Minecraft-Version/Loader nicht erkennbar — bitte den "
                      "Server manuell erstellen (Tab „Server“) oder die Server "
                      "Files über „Server importieren“ einbinden.")
    elif loader not in ALLOWED_LOADERS:
        errors.append(f"Loader '{loader}' wird für Server nicht unterstützt "
                      f"({', '.join(ALLOWED_LOADERS)}).")
    if fmt == "curseforge" and mods:
        warnings.append(
            f"CurseForge-Client-Export: {mods} Mods werden einzeln von "
            f"CurseForge geladen. Schneller und zuverlässiger sind die "
            f"„Server Files“ des Packs (CurseForge-Projektseite, Reiter Files, "
            f"Additional Files).")
    host = host_memory_bytes()
    if memory and host and _memory_bytes(memory) + 1024 ** 3 > host:
        warnings.append(
            f"Empfohlen sind {memory} RAM für den Server, der Host hat "
            f"insgesamt nur {host / 1024 ** 3:.1f} GB — der Server könnte "
            f"abstürzen oder den Host ausbremsen.")
    try:
        size = archive.stat().st_size
        free = shutil.disk_usage(settings.instances_dir).free
        if free < size * 3 + _SPACE_BUFFER:
            warnings.append(f"Wenig Speicherplatz: frei sind "
                            f"{free // 1024 ** 2} MiB, benötigt werden etwa "
                            f"{(size * 3 + _SPACE_BUFFER) // 1024 ** 2} MiB.")
    except OSError:
        size = 0
    version = None
    if fmt == "curseforge":
        version = raw.get("version")
    elif fmt == "modrinth":
        version = raw.get("versionId")
    java = runtime.java_tag({"game_version": game_version}).removeprefix("java") \
        if game_version else None
    return {
        "filename": filename,
        "size": size,
        "format": fmt,
        "title": pack.get("title") or _title_from_filename(filename),
        "version": str(version) if version else None,
        "game_version": game_version,
        "loader": loader,
        "loader_version": pack.get("loader_version") or None,
        "detected_by": pack.get("detected_by"),
        "java": java,
        "mods": mods,
        "recommended_memory": memory,
        "default_memory": settings.instances_memory,
        "host_memory": host,
        "warnings": warnings,
        "errors": errors,
    }


def _cleanup_previews() -> None:
    now = time.time()
    for path in staging_dir().glob(f"{_PREVIEW_PREFIX}*"):
        try:
            if now - path.stat().st_mtime > _PREVIEW_MAX_AGE:
                path.unlink()
        except OSError:
            pass


def preview_path(filename: str) -> tuple:
    """Neuer Ablageort für einen Vorschau-Upload: (token, Pfad)."""
    _cleanup_previews()
    token = uuid.uuid4().hex[:16]
    return token, staging_dir() / f"{_PREVIEW_PREFIX}{token}_{filename}"


def staged_preview(token: str) -> tuple:
    """(Pfad, Dateiname) eines Vorschau-Uploads; 404, wenn abgelaufen."""
    token = str(token or "").strip()
    if not _PREVIEW_TOKEN_RE.match(token):
        raise HTTPException(status_code=400, detail="Ungültige Upload-Kennung")
    prefix = f"{_PREVIEW_PREFIX}{token}_"
    for path in staging_dir().glob(f"{prefix}*"):
        return path, path.name[len(prefix):]
    raise HTTPException(status_code=404,
                        detail="Hochgeladene Datei nicht mehr vorhanden — "
                               "bitte erneut hochladen")


async def create_server_from_upload(archive: Path, filename: str, name: str = None,
                                    memory: str = None, port: int = None,
                                    accept_eula: bool = False) -> dict:
    """Erstellt aus einem hochgeladenen Modpack-Archiv (.mrpack/.zip/Server
    Files) direkt eine neue Server-Instanz und installiert das Pack dort.
    Ohne RAM-Angabe wird die Empfehlung nach Mod-Anzahl genutzt."""
    instance = None
    try:
        loader, game_version, title, mods = await asyncio.to_thread(
            _meta_from_archive, archive)
        if not memory:
            memory = recommend_memory(mods)
        instance = create_instance(
            _unique_name(_sanitize_name(name or title or _title_from_filename(filename)
                                        or "Hochgeladenes Modpack")),
            loader, game_version, loader_version=None, memory=memory,
            port=port, accept_eula=accept_eula)
        dest = pack_dir(instance["id"]) / filename
        shutil.move(str(archive), str(dest))
        job = await install_upload(instance["id"], dest, filename)
        return {"instance": instance, "job": job}
    except Exception:
        if instance is not None:
            shutil.rmtree(instance_dir(instance["id"]), ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# Modpack-Updates (neuere Pack-Version in bestehende Instanz installieren)
# ---------------------------------------------------------------------------

def _pack_source(mp: dict) -> str:
    """Quelle des installierten Packs: 'modrinth' (Suche, ohne source-Feld),
    'curseforge' oder 'upload' (hochgeladenes Archiv)."""
    source = str(mp.get("source") or "").strip().lower()
    if source:
        return source
    return "modrinth"


def _primary_modrinth_file(version: dict):
    """Primäre Datei einer Modrinth-Version (primary-Flag, sonst die erste)."""
    files = version.get("files") or []
    return next((f for f in files if f.get("primary")),
                files[0] if files else None)


def _first_compatible_modrinth_version(versions: list, loader: str,
                                       game_version: str):
    """Erste (neueste) Modrinth-Version, die zur Loader/MC-Kombi passt.
    Eine Quelle für Install-Pfad und Update-Check, damit beide nie
    auseinanderlaufen."""
    for version in versions or []:
        if not isinstance(version, dict) or not version.get("id"):
            continue
        if loader in [str(x).lower() for x in version.get("loaders") or []] \
                and game_version in [str(x) for x in version.get("game_versions") or []]:
            return version
    return None


def _version_brief_modrinth(v: dict) -> dict:
    primary = _primary_modrinth_file(v)
    return {
        "version_id": str(v.get("id")),
        "name": str(v.get("name") or v.get("version_number") or v.get("id")),
        "date": str(v.get("date_published") or ""),
        "size": int((primary or {}).get("size") or 0),
    }


def _cf_compatibility(instance: dict, entry: dict) -> bool | None:
    """Kompatibilität einer CF-Pack-Datei zur Instanz: True/False aus den
    gameVersions-Tags, None wenn die Datei keine Tags deklariert."""
    versions_list = entry.get("game_versions") or []
    loaders = entry.get("loaders") or []
    if not versions_list and not loaders:
        return None
    gv_ok = not versions_list or instance["game_version"] in versions_list
    ld_ok = not loaders or instance["loader"] in loaders
    return gv_ok and ld_ok


# Update-Check-Cache: (Instanz, Pack-Stand) → (Zeitpunkt, Ergebnis). Das
# Öffnen eines Servers fragt so nicht jedes Mal Modrinth/CurseForge.
_UPDATE_CHECK_TTL = 3600.0
_update_check_cache: dict = {}


async def pack_update_check_cached(instance_id: str, refresh: bool = False) -> dict:
    """pack_update_check mit 1-h-Cache; refresh=True fragt neu an. Der
    Schlüssel enthält den installierten Stand, ein Update/Versionswechsel
    macht den Eintrag damit automatisch ungültig."""
    instance = get_instance(instance_id)
    mp = instance.get("modpack") or {}
    key = (instance_id, str(mp.get("project_id") or ""), str(mp.get("version_id") or ""),
           instance.get("loader"), instance.get("game_version"))
    now = time.monotonic()
    hit = _update_check_cache.get(key)
    if hit and not refresh and now - hit[0] < _UPDATE_CHECK_TTL:
        return {**hit[1], "cached": True}
    result = await pack_update_check(instance_id)
    _update_check_cache[key] = (now, result)
    return result


async def pack_update_check(instance_id: str) -> dict:
    """Prüft, ob für das installierte Modpack eine neuere Version existiert.
    Wirft nicht für 'nicht prüfbar' (Upload ohne Projekt-Quelle) — liefert
    stattdessen checkable=False mit Grund."""
    instance = get_instance(instance_id)
    mp = instance.get("modpack") or {}
    out: dict = {"installed": bool(mp), "checkable": False, "source": None,
                 "project_id": None, "installed_pack": None, "latest": None,
                 "compatible": None, "update_available": False, "reason": None}
    if not mp:
        out["reason"] = "Kein Modpack installiert"
        return out
    project_id = str(mp.get("project_id") or "")
    source = _pack_source(mp)
    out["source"] = source
    out["project_id"] = project_id or None
    installed_pack = {
        "version_id": mp.get("version_id"),
        "name": mp.get("title") or "",
    }
    if not project_id or source == "upload":
        out["reason"] = ("Hochgeladenes Archiv ohne Projekt-Quelle — "
                         "Update-Check nicht möglich (neues Pack hochladen oder suchen)")
        return out
    out["checkable"] = True

    if source == "curseforge":
        from . import curseforge  # lazy, vermeidet Import-Zirkel
        curseforge._require_key()
        data = await curseforge.list_pack_versions(project_id)
        versions = data.get("versions") or []
        installed = next((v for v in versions
                          if str(v.get("file_id")) == str(mp.get("version_id"))), None)
        if installed:
            installed_pack.update({"name": installed.get("name") or installed_pack["name"],
                                   "date": installed.get("date") or ""})
        # Neueste stabile Datei (Beta/Alpha nur, wenn es nichts Stabiles gibt)
        latest = next((v for v in versions if v.get("release") == 1),
                      versions[0] if versions else None)
        if latest:
            brief = {"version_id": latest.get("file_id"),
                     "name": latest.get("name") or "",
                     "date": latest.get("date") or "",
                     "size": int(latest.get("size") or 0),
                     "release": latest.get("release")}
            out["latest"] = brief
            out["compatible"] = _cf_compatibility(instance, latest)
            installed_id = str(mp.get("version_id") or "")
            # Ohne persistierte Versions-ID (ältere CF-Installationen) gilt
            # "unbekannt" — einmal aktualisieren pinnt den Stand (Root-Fix:
            # install_pack_cf speichert die gewählte Datei-ID jetzt selbst).
            out["update_available"] = (
                out["compatible"] is not False
                and (not installed_id
                     or str(latest.get("file_id")) != installed_id))
    else:
        async with _new_client() as client:
            versions = await modrinth._get_json(
                client, f"/project/{project_id}/version")
        installed = next((v for v in versions or []
                          if isinstance(v, dict)
                          and str(v.get("id")) == str(mp.get("version_id"))), None)
        if installed:
            installed_pack.update(_version_brief_modrinth(installed))
        latest = _first_compatible_modrinth_version(versions or [],
                                                    instance["loader"],
                                                    instance["game_version"])
        if latest:
            out["latest"] = _version_brief_modrinth(latest)
            out["compatible"] = True
            out["update_available"] = (str(latest.get("id"))
                                       != str(mp.get("version_id")))
        elif versions:
            out["compatible"] = False
            out["reason"] = (f"Keine Pack-Version kompatibel mit "
                             f"{instance['loader']} {instance['game_version']}")
        else:
            out["reason"] = "Modpack-Versionen nicht gefunden"
    out["installed_pack"] = installed_pack
    return out


async def pack_update(instance_id: str, version_id: str = None,
                      file_id: str = None) -> dict:
    """Installiert eine (standardmäßig die neueste) Pack-Version erneut in
    die bestehende Instanz — wie eine Installation mit force=true."""
    instance = get_instance(instance_id)
    mp = instance.get("modpack") or {}
    project_id = str(mp.get("project_id") or "")
    source = _pack_source(mp)
    if not project_id or source == "upload":
        raise HTTPException(
            status_code=400,
            detail=("Kein update-fähiges Modpack installiert "
                    "(hochgeladene Archive ohne Projekt-Quelle können "
                    "nicht geprüft werden)"))
    if source == "curseforge":
        return await install_pack_cf(instance_id, project_id,
                                     file_id=file_id, force=True)
    return await install_pack(instance_id, project_id, version_id=version_id,
                              force=True)


__all__ = [
    "_check_compatibility",
    "_file_plan",
    "analyze_archive",
    "create_server_from_pack",
    "create_server_from_upload",
    "install_pack",
    "install_upload",
    "list_pack_versions",
    "pack_update",
    "pack_update_check",
    "preview_path",
    "recommend_memory",
    "retry_failed",
    "search_modpacks",
    "search_modpacks_global",
    "staged_preview",
]
