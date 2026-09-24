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
import json
import os
import re
import shutil
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
    if not isinstance(pack, dict) or not pack.get("files"):
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
            raise HTTPException(
                status_code=400,
                detail=("Kein bekanntes Modpack-Format gefunden "
                        "(modrinth.index.json oder manifest.json fehlen)"))
    except (zipfile.BadZipFile, ValueError) as exc:
        raise HTTPException(status_code=400,
                            detail=f"Modpack-Archiv beschädigt: {exc}") from exc
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f"Modpack nicht entpackbar: {exc}") from exc


def _normalize_index(fmt: str, index: dict) -> dict:
    """Vereinheitlicht mrpack- und CurseForge-Index auf ein internes Schema:
    {title, game_version, loader, loader_version,
     files: [{path, downloads, fileSize, sha1}]}"""
    if not isinstance(index, dict):
        raise HTTPException(status_code=400, detail="Modpack-Index ist beschädigt")
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


async def _run_upload_install(job: dict, instance: dict, dest: Path,
                              source: str = "upload",
                              auto_version: bool = False) -> None:
    """Hintergrund-Job für Uploads: Index lesen → prüfen → Dateien installieren.

    auto_version=True: Bei abweichender MC-Version/Loader-Kombination wird die
    Instanz automatisch umgestellt (nur Upload-Pfad; CF-Suche bleibt streng).
    """
    root = instance_dir(instance["id"])
    mods_root = root / "mods"
    failures: list[str] = []
    try:
        job["phase"] = "Prüfung (Kompatibilität)"
        fmt, raw_index = _extract_index(dest)
        pack = _normalize_index(fmt, raw_index)
        adapted = None
        if auto_version and _needs_version_adaptation(instance, pack):
            loader_dep = _adapt_instance_for_pack(instance, pack)
            adapted = {"from": loader_dep["from"], "to": loader_dep["to"]}
            job["phase"] = _adapt_phase_note(adapted)
        else:
            loader_dep = _check_compatibility(instance, pack)

        skipped, plan, total_bytes, jobs_total = [], [], 0, 0
        if fmt == "modrinth":
            plan, skipped = _file_plan(root, pack)
            total_bytes = sum(size for _, _, size, _ in plan)
            jobs_total = len(plan)
        else:
            job["phase"] = "Overrides extrahieren"
            _, over_skip = await asyncio.to_thread(_extract_overrides, dest, root)
            skipped.extend(over_skip)
            jobs_total = len(pack["files"])

        job["phase"] = "Prüfung (Speicherplatz)"
        _check_disk_space_job(root, total_bytes)

        job["status"] = "installing"
        if fmt == "modrinth":
            # Archiv-Größe bereits erledigt (Upload) + bekannte Mod-Größe
            job["total"] = job["downloaded"] + total_bytes
        else:
            job["total"] = 0  # CurseForge: Mod-Größen unbekannt → nur Phase zeigen
        job["phase"] = f"Mods installieren (0/{jobs_total})"

        sem = asyncio.Semaphore(_CONCURRENCY)
        completed = {"count": 0}
        installed = {"n": 0}

        def _progress():
            completed["count"] += 1
            job["phase"] = f"Mods installieren ({completed['count']}/{jobs_total})"

        async def mr_worker(item):
            u, d, s, h = item
            async with sem:
                try:
                    await _download_one(dl_client, u, d, s, h)
                    job["downloaded"] += s
                    installed["n"] += 1
                except Exception as exc:
                    failures.append(f"{d.name}: {exc}")
                finally:
                    _progress()

        async def cf_worker(entry):
            url = (entry.get("downloads") or [""])[0]
            async with sem:
                try:
                    name = await _download_cf_one(dl_client, url, mods_root)
                    if name is None:
                        skipped.append(f"CurseForge-Datei {url.rsplit('/', 1)[-1] if url else '?'} "
                                       f"(kein .jar oder bekannte Client-only-Mod)")
                    else:
                        installed["n"] += 1
                        try:
                            job["downloaded"] += (mods_root / name).stat().st_size
                        except OSError:
                            pass
                except Exception as exc:
                    label = url.rsplit("/", 1)[-1] or url or "?"
                    failures.append(
                        f"CurseForge-Download fehlgeschlagen ({label}): {exc}")
                finally:
                    _progress()

        async with _new_client() as dl_client:
            if fmt == "modrinth":
                await asyncio.gather(*(mr_worker(item) for item in plan))
            else:
                await asyncio.gather(*(cf_worker(f) for f in pack["files"]))
        if failures:
            shown = "; ".join(failures[:5])
            more = (f" … +{len(failures) - 5} weitere"
                    if len(failures) > 5 else "")
            raise RuntimeError(f"{len(failures)} Mod-Datei(en) fehlgeschlagen: "
                               f"{shown}{more}")

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
            "files": installed["n"],
            "skipped": skipped,
        }
        if adapted:
            instance["modpack"]["version_adapted"] = adapted
        if loader_dep["version"]:
            instance["loader_version"] = loader_dep["version"]
        update_instance(instance)
        job["status"] = "done"
        job["phase"] = "Fertig"
        job["summary"] = {"files": installed["n"], "skipped": skipped}
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
    """(loader, game_version, title) aus einem hochgeladenen Pack-Archiv."""
    fmt, raw_index = _extract_index(archive)
    pack = _normalize_index(fmt, raw_index)
    loader, game_version = pack.get("loader"), pack.get("game_version")
    if not loader or not game_version:
        raise HTTPException(
            status_code=400,
            detail=("Modpack deklariert Loader/Minecraft-Version nicht eindeutig — "
                    "bitte Server manuell erstellen (Tab „Server“)"))
    return loader, str(game_version), pack.get("title")


async def create_server_from_upload(archive: Path, filename: str, name: str = None,
                                    memory: str = None, port: int = None,
                                    accept_eula: bool = False) -> dict:
    """Erstellt aus einem hochgeladenen Modpack-Archiv (.mrpack/.zip) direkt
    eine neue Server-Instanz und installiert das Pack dort."""
    instance = None
    try:
        loader, game_version, title = _meta_from_archive(archive)
        instance = create_instance(
            _unique_name(_sanitize_name(name or title or "Hochgeladenes Modpack")),
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
    "create_server_from_pack",
    "create_server_from_upload",
    "install_pack",
    "install_upload",
    "list_pack_versions",
    "pack_update",
    "pack_update_check",
    "search_modpacks",
    "search_modpacks_global",
]
