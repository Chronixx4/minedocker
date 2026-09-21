"""CurseForge-API-Client (v1) für Mod-/Modpack-Suche und Direktdownload.

Die CurseForge-API erfordert einen kostenlosen API-Key (https://console.curseforge.com),
der über CF_API_KEY gesetzt wird. Ohne Key antworten die Endpunkte mit 503.

Auflösung von Projekten:
  - numerische Mod-ID (z. B. "238222")  -> GET /mods/{id}
  - Slug (z. B. "jei")                  -> GET /mods/search?slug=...
"""
import re

import httpx
from fastapi import HTTPException

from .config import settings
from .security import validate_curseforge_download_url

# MC-Version-Schema (z. B. "1.20.1", "1.21", "1.20.1-rc1")
MC_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?(-[A-Za-z0-9.]+)?$")

GAME_ID = 432  # Minecraft bei CurseForge
CLASS_MOD = 6
CLASS_MODPACK = 4471

# CF-ModLoaderType-Enum (Any=0, Forge=1, LiteLoader=2, Fabric=4, Quilt=5, NeoForge=6)
_LOADER_TYPES = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NUMERIC_RE = re.compile(r"^\d{1,12}$")

# Fallback für Dateien ohne downloadUrl (drittanbieter-Download gesperrt):
# Website-Redirect liefert den Dateinamen aus der finalen CDN-URL.
DOWNLOAD_URL_TEMPLATE = "https://www.curseforge.com/api/v1/mods/{pid}/files/{fid}/download"


def _headers() -> dict:
    headers = {"User-Agent": settings.user_agent, "Accept": "application/json"}
    if settings.cf_api_key:
        headers["x-api-key"] = settings.cf_api_key
    return headers


def _require_key() -> str:
    if not settings.cf_api_key:
        raise HTTPException(
            status_code=503,
            detail=("CF_API_KEY ist nicht gesetzt. CurseForge verlangt einen "
                    "kostenlosen API-Key: https://console.curseforge.com "
                    "(Umgebungsvariable CF_API_KEY)"))
    return settings.cf_api_key


def _validate_id(value: str, what: str) -> str:
    if not value or not _ID_RE.match(value):
        raise HTTPException(status_code=400, detail=f"Ungültiger Wert für {what}")
    return value


def _new_client(**kwargs) -> httpx.AsyncClient:
    """Client-Fabrik (Tests injizieren hier einen MockTransport)."""
    kwargs.setdefault("timeout", httpx.Timeout(15.0, read=120.0))
    kwargs.setdefault("follow_redirects", True)
    return httpx.AsyncClient(**kwargs)


async def _get_json(client: httpx.AsyncClient, path: str,
                    params: dict | None = None):
    try:
        resp = await client.get(f"{settings.curseforge_api}{path}",
                                params=params or {}, headers=_headers())
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502,
                            detail=f"CurseForge nicht erreichbar ({exc.__class__.__name__})")
    if resp.status_code == 404:
        raise HTTPException(status_code=404,
                            detail="Projekt/Datei bei CurseForge nicht gefunden")
    if resp.status_code != 200:
        raise HTTPException(status_code=502,
                            detail=f"CurseForge antwortete mit HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="CurseForge-Antwort unlesbar") from exc


def _hit(mod: dict) -> dict:
    """CF-Mod-Objekt auf das Modrinth-Suchergebnis-Schema normalisieren."""
    authors = [a.get("name") for a in (mod.get("authors") or []) if isinstance(a, dict)]
    logo = mod.get("logo") or {}
    return {
        "project_id": str(mod.get("id") or ""),
        "slug": mod.get("slug") or "",
        "title": mod.get("name") or "",
        "author": authors[0] if authors else "",
        "description": mod.get("summary") or "",
        "downloads": int(mod.get("downloadCount") or 0),
        "icon_url": logo.get("thumbnailUrl") or logo.get("url") or "",
        "latest_version": "",
        "server_side": None,  # CurseForge deklariert Server-Abhängigkeit nicht
        "class_id": mod.get("classId"),
        "date_modified": mod.get("dateModified") or "",
    }


async def _resolve_project(client: httpx.AsyncClient, project_id: str) -> dict:
    """Numerische Mod-ID oder Slug auf ein vollständiges CF-Mod-Objekt auflösen."""
    _validate_id(project_id, "Projekt-ID")
    if _NUMERIC_RE.match(project_id):
        data = await _get_json(client, f"/mods/{project_id}")
        mod = data.get("data") if isinstance(data, dict) else None
        if not isinstance(mod, dict) or not mod.get("id"):
            raise HTTPException(status_code=404, detail="Projekt bei CurseForge nicht gefunden")
        return mod
    data = await _get_json(client, "/mods/search", params={
        "gameId": GAME_ID, "slug": project_id, "pageSize": 5,
    })
    hits = (data.get("data") or []) if isinstance(data, dict) else []
    if not hits:
        raise HTTPException(status_code=404,
                            detail=f"Kein CurseForge-Projekt mit Slug '{project_id}'")
    return hits[0]


# CF-sortField-Werte: Relevanz→Popularity, Downloads→TotalDownloads,
# Aktualisiert→LastUpdated, Neueste→DateCreated
_SORT_FIELDS = {"relevance": 2, "downloads": 6, "updated": 3, "newest": 9}


async def search_mods(query: str, loader: str | None, game_version: str | None,
                      offset: int = 0, sort: str = "relevance") -> dict:
    """Sucht Mods (classId=6), optional gefiltert nach MC-Version und ModLoader-Typ.

    loader=None bzw. game_version=None lässt den jeweiligen Filter weg."""
    _require_key()
    if sort not in _SORT_FIELDS:
        raise HTTPException(status_code=400,
                            detail=f"Sortierung muss einer von {', '.join(_SORT_FIELDS)} sein")
    params = {
        "gameId": GAME_ID,
        "classId": CLASS_MOD,
        "sortField": _SORT_FIELDS[sort],
        "sortOrder": "desc",
        "pageSize": 20,
        "pageNumber": max(0, offset) // 20,
    }
    if loader is not None:
        if loader not in _LOADER_TYPES:
            raise HTTPException(
                status_code=400,
                detail=(f"CurseForge-Suche unterstützt nur die Loader "
                        f"{', '.join(sorted(_LOADER_TYPES))} (nicht '{loader}')"))
        params["modLoaderType"] = _LOADER_TYPES[loader]
    if game_version is not None:
        params["gameVersion"] = game_version
    if (query or "").strip():
        params["searchFilter"] = query.strip()[:100]
    async with _new_client() as client:
        data = await _get_json(client, "/mods/search", params=params)
    hits = [_hit(m) for m in (data.get("data") or []) if isinstance(m, dict)]
    pagination = data.get("pagination") or {}
    return {
        "total": int(pagination.get("totalCount") or len(hits)),
        "hits": hits,
        "loader": loader,
        "game_version": game_version,
        "source": "curseforge",
    }


async def search_modpacks(instance: dict, query: str, offset: int = 0) -> dict:
    """Sucht Modpacks (classId=4471) passend zur MC-Version der Instanz.

    Die Loader-Kompatibilität kann CurseForge in der Suche nicht zuverlässig
    liefern; sie wird erst bei der Installation über das manifest.json geprüft
    (daher compatible=None = 'wird bei der Installation geprüft').
    """
    _require_key()
    params = {
        "gameId": GAME_ID,
        "classId": CLASS_MODPACK,
        "gameVersion": instance["game_version"],
        "sortField": 2,  # Popularity
        "sortOrder": "desc",
        "pageSize": 20,
        "pageNumber": max(0, offset) // 20,
    }
    if (query or "").strip():
        params["searchFilter"] = query.strip()[:100]
    async with _new_client() as client:
        data = await _get_json(client, "/mods/search", params=params)
    hits = []
    for m in (data.get("data") or []):
        if not isinstance(m, dict):
            continue
        entry = _hit(m)
        entry["loaders"] = []      # deklariert CurseForge-Suche nicht
        entry["versions"] = [instance["game_version"]]
        entry["compatible"] = None
        hits.append(entry)
    pagination = data.get("pagination") or {}
    return {
        "total": int(pagination.get("totalCount") or len(hits)),
        "hits": hits,
        "instance": {"id": instance["id"], "loader": instance["loader"],
                     "game_version": instance["game_version"]},
        "source": "curseforge",
    }


def _file_url(file: dict, mod_id: int) -> str:
    """Download-URL einer Datei; Fallback auf Website-Redirect ohne Key.
    downloadUrl wird auf CurseForge-CDN-Hosts beschränkt (Anti-SSRF) —
    abweichende Hosts fallen auf den regulären Redirect zurück."""
    url = str(file.get("downloadUrl") or "")
    if url.startswith("https://"):
        try:
            validate_curseforge_download_url(url)
            return url
        except RuntimeError:
            pass  # Fremd-Host → Website-Redirect-Fallback
    return DOWNLOAD_URL_TEMPLATE.format(pid=mod_id, fid=file.get("id"))


# CF-Relationstypen: 1=Embedded, 2=Optional, 3=Required, 4=Tool, 5=Include,
# 6=Exclude — nur Required wird automatisch mitinstalliert.
RELATION_REQUIRED = 3


def required_dep_ids(file: dict) -> list[str]:
    """Pflicht-Abhängigkeiten (relationType=3) einer CF-Datei als Mod-IDs.

    CurseForge liefert das 'relations'-Feld nicht für alle Dateien — fehlt
    es oder ist es unlesbar, ist die Liste leer (best effort, wirft nie)."""
    relations = (file or {}).get("relations")
    projects = relations.get("projects") if isinstance(relations, dict) else None
    out: list[str] = []
    for entry in projects or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        try:
            if int(entry.get("relationType") or 0) == RELATION_REQUIRED:
                out.append(str(entry["id"]))
        except (TypeError, ValueError):
            continue
    return out


def _files_params(loader: str, game_version: str | None) -> dict:
    """Filter-Parameter für /mods/{id}/files (MC-Version + Loader-Typ)."""
    params: dict[str, str | int] = {"pageSize": 100}
    if game_version:
        params["gameVersion"] = game_version
    if (loader or "").lower() in _LOADER_TYPES:
        params["modLoaderType"] = _LOADER_TYPES[(loader or "").lower()]
    return params


def _pick_file(files: list, jar_preferred: bool = False) -> dict:
    """Neueste stabile Datei wählen (CurseForge liefert serverseitig gefilterte
    Listen); bei Mods werden Nicht-.jar-Dateien (Sources etc.) bevorzugt übersprungen."""
    candidates = [f for f in (files or []) if isinstance(f, dict) and f.get("fileName")]
    if not candidates:
        raise HTTPException(status_code=404, detail="Keine CurseForge-Datei gefunden")
    if jar_preferred:
        jar_only = [f for f in candidates
                    if str(f.get("fileName") or "").lower().endswith(".jar")]
        candidates = jar_only or candidates
    releases = [f for f in candidates if f.get("releaseType") == 1]
    pool = releases or candidates

    def sort_key(f):
        date = str(f.get("fileDate") or "")
        try:
            fid = int(f.get("id") or 0)
        except (TypeError, ValueError):
            fid = 0
        return (date, fid)

    return max(pool, key=sort_key)


async def resolve_download_full(project_id: str, file_id: str | None,
                                loader: str, game_version: str) -> tuple[str, str, int, str | None, list[str]]:
    """Löst ein CF-Mod zur neuesten kompatiblen Datei auf:
    (Dateiname, Download-URL, Größe in Bytes, SHA1, Pflicht-Abhängigkeiten
    als Mod-IDs — best effort, CF liefert das Relations-Feld nicht bei
    allen Dateien)."""
    _require_key()
    loader = (loader or "").lower()
    async with _new_client() as client:
        mod = await _resolve_project(client, project_id)
        mod_id = int(mod.get("id") or 0)
        params: dict[str, str | int] = {"pageSize": 100}
        if game_version:
            params["gameVersion"] = game_version
        if loader in _LOADER_TYPES:
            params["modLoaderType"] = _LOADER_TYPES[loader]
        data = await _get_json(client, f"/mods/{mod_id}/files", params=params)
        files = (data.get("data") or []) if isinstance(data, dict) else []
        if file_id:
            if not _NUMERIC_RE.match(file_id):
                raise HTTPException(status_code=400, detail="Ungültige Datei-ID")
            matches = [f for f in files if str(f.get("id")) == file_id]
            if not matches:
                raise HTTPException(
                    status_code=404,
                    detail=(f"Datei {file_id} gehört nicht zu diesem Projekt oder "
                            f"passt nicht zu {loader} {game_version}"))
            chosen = matches[0]
        else:
            chosen = _pick_file(files, jar_preferred=True)
    size = int(chosen.get("fileSize") or 0)
    sha1 = (chosen.get("hashes") or {}).get("sha1") or None
    return (str(chosen["fileName"]), _file_url(chosen, mod_id), size, sha1,
            required_dep_ids(chosen))


async def resolve_download(project_id: str, file_id: str | None,
                           loader: str, game_version: str) -> tuple[str, str, int, str | None]:
    """Löst ein CF-Mod zur neuesten kompatiblen Datei auf:
    (Dateiname, Download-URL, Größe in Bytes, SHA1)."""
    filename, url, size, sha1, _deps = await resolve_download_full(
        project_id, file_id, loader, game_version)
    return filename, url, size, sha1


# Abhängigkeits-Auflösung (CurseForge): gleiche Grenzen wie bei Modrinth.
_MAX_DEPS = 20
_DEP_DEPTH = 2


async def dependency_files(dep_ids: list[str], loader: str, game_version: str,
                           installed_mod_ids: set | None = None) -> dict:
    """Löst CurseForge-Pflicht-Abhängigkeiten (Mod-IDs aus dem Relations-Feld)
    zu Download-Einträgen auf — rekursiv bis Tiefe 2, max _MAX_DEPS Dateien.
    Bereits installierte Mod-IDs werden übersprungen. Rückgabe (wie bei
    Modrinth): {'files': [{filename, url, size, sha1, project_id,
    version_id}], 'skipped': [{project_id, reason}]}."""
    loader = (loader or "").lower()
    visited: set = set()
    files: list = []
    skipped: list = []
    queue = [(str(dep_id), 0) for dep_id in dep_ids]
    async with _new_client() as client:
        while queue and len(files) < _MAX_DEPS:
            dep_id, depth = queue.pop(0)
            if not dep_id or dep_id in visited:
                continue
            visited.add(dep_id)
            if installed_mod_ids and dep_id in installed_mod_ids:
                skipped.append({"project_id": dep_id,
                                "reason": "bereits installiert"})
                continue
            try:
                mod = await _resolve_project(client, dep_id)
                mod_id = int(mod.get("id") or 0)
                data = await _get_json(
                    client, f"/mods/{mod_id}/files",
                    params=_files_params(loader, game_version))
                dep_files = (data.get("data") or []) if isinstance(data, dict) else []
                dep_file = _pick_file(dep_files, jar_preferred=True)
            except (HTTPException, TypeError, ValueError):
                skipped.append({"project_id": dep_id,
                                "reason": "keine kompatible Datei"})
                continue
            files.append({
                "filename": str(dep_file["fileName"]),
                "url": _file_url(dep_file, mod_id),
                "size": int(dep_file.get("fileSize") or 0),
                "sha1": (dep_file.get("hashes") or {}).get("sha1") or None,
                "project_id": str(mod_id),
                "version_id": str(dep_file.get("id") or ""),
            })
            if depth + 1 < _DEP_DEPTH:
                for sub_id in required_dep_ids(dep_file):
                    queue.append((sub_id, depth + 1))
    return {"files": files, "skipped": skipped}


async def resolve_pack_file(project_id: str, file_id: str | None = None,
                            server_pack: bool = False) -> tuple[dict, dict, dict | None]:
    """Löst ein CF-Modpack auf: (Mod-Objekt, gewählte Pack-Datei, Server-Pack-Datei).

    Gewählt wird die neueste stabile Pack-Datei (oder file_id). CurseForge
    liefert bei Modpacks zusätzlich `serverPackFileId` — die separate
    Server-Pack-Datei (enthält serverseitige Dateien ohne Client-Mods).
    Mit server_pack=True wird diese zurückgegeben, wenn sie existiert.
    """
    _require_key()
    async with _new_client() as client:
        mod = await _resolve_project(client, project_id)
        mod_id = mod.get("id")
        data = await _get_json(client, f"/mods/{mod_id}/files",
                               params={"pageSize": 100})
        files = (data.get("data") or []) if isinstance(data, dict) else []
        if file_id:
            if not _NUMERIC_RE.match(file_id):
                raise HTTPException(status_code=400, detail="Ungültige Datei-ID")
            matches = [f for f in files if str(f.get("id")) == file_id]
            if not matches:
                raise HTTPException(
                    status_code=404,
                    detail=f"Datei {file_id} gehört nicht zu diesem Modpack")
            chosen = matches[0]
        else:
            chosen = _pick_file(files)
        server_file = None
        if server_pack:
            spid = chosen.get("serverPackFileId")
            if spid:
                server_file = next(
                    (f for f in files if str(f.get("id")) == str(spid)), None)
    return mod, chosen, server_file


def pack_meta_from_file(file: dict) -> tuple[str | None, str | None]:
    """(MC-Version, Loader) aus den gameVersions-Tags einer Pack-Datei,
    z. B. ["1.20.1", "Forge"] -> ("1.20.1", "forge")."""
    game_version, loader = None, None
    for tag in (file or {}).get("gameVersions") or []:
        tag = str(tag).strip()
        lowered = tag.lower()
        if loader is None and lowered in _LOADER_TYPES:
            loader = lowered
        elif game_version is None and MC_VERSION_RE.match(tag):
            game_version = tag
    return game_version, loader


def file_version_tags(file: dict) -> tuple[list, list]:
    """Alle MC-Version- und Loader-Tags einer Datei: (versionen, loader)."""
    versions, loaders = [], set()
    for tag in (file or {}).get("gameVersions") or []:
        tag = str(tag).strip()
        lowered = tag.lower()
        if lowered in _LOADER_TYPES:
            loaders.add(lowered)
        elif MC_VERSION_RE.match(tag):
            versions.append(tag)
    return versions, sorted(loaders)


_RELEASE_LABELS = {1: "Stabil", 2: "Beta", 3: "Alpha"}


async def list_pack_versions(project_id: str) -> dict:
    """Alle Pack-Dateien eines CF-Modpacks (für die Versionswahl):
    Dateiname, Tags, Server-Pack-Verfügbarkeit, Release-Typ."""
    _require_key()
    async with _new_client() as client:
        mod = await _resolve_project(client, project_id)
        mod_id = mod.get("id")
        data = await _get_json(client, f"/mods/{mod_id}/files",
                               params={"pageSize": 100})
        files = (data.get("data") or []) if isinstance(data, dict) else []
    versions = []
    for f in files:
        if not isinstance(f, dict) or not f.get("id"):
            continue
        versions_list, loaders = file_version_tags(f)
        versions.append({
            "file_id": str(f.get("id")),
            "name": str(f.get("fileName") or f"Datei {f.get('id')}"),
            "loaders": loaders,
            "game_versions": versions_list,
            "date": str(f.get("fileDate") or ""),
            "size": int(f.get("fileSize") or 0),
            "release": int(f.get("releaseType") or 0),
            "release_label": _RELEASE_LABELS.get(int(f.get("releaseType") or 0), ""),
            "server_pack": bool(f.get("serverPackFileId")),
        })
    versions.sort(key=lambda v: (v["date"], v["file_id"]), reverse=True)
    return {"source": "curseforge", "project_id": project_id, "versions": versions}


async def resolve_pack(project_id: str,
                       file_id: str | None = None
                       ) -> tuple[dict, str, str, int, str | None, str]:
    """Löst ein CF-Modpack auf: (Mod-Objekt, Dateiname, Download-URL, Größe,
    SHA1, gewählte Datei-ID). Pack-Dateien werden nicht nach Loader gefiltert —
    das manifest.json entscheidet über Kompatibilität (wird beim Install
    geprüft)."""
    mod, chosen, _ = await resolve_pack_file(project_id, file_id)
    filename = str(chosen["fileName"])
    if not filename.lower().endswith(".zip"):
        filename += ".zip"
    size = int(chosen.get("fileSize") or 0)
    sha1 = (chosen.get("hashes") or {}).get("sha1") or None
    file_id_chosen = str(chosen.get("id") or "")
    return (mod, filename, _file_url(chosen, int(mod.get("id") or 0)),
            size, sha1, file_id_chosen)


async def search_modpacks_global(query: str, offset: int = 0,
                                 loader: str = None,
                                 game_version: str = None) -> dict:
    """Globale Modpack-Suche (ohne Instanzbezug) — für 'Server aus Modpack
    erstellen'. Optional nach MC-Version und Loader filtern; Loader/MC-Versionen
    der Treffer werden aus latestFilesIndexes abgeleitet."""
    _require_key()
    params = {
        "gameId": GAME_ID,
        "classId": CLASS_MODPACK,
        "sortField": 2,  # Popularity
        "sortOrder": "desc",
        "pageSize": 20,
        "pageNumber": max(0, offset) // 20,
    }
    if game_version:
        params["gameVersion"] = game_version
    if loader:
        loader = (loader or "").strip().lower()
        if loader not in _LOADER_TYPES:
            raise HTTPException(
                status_code=400,
                detail=(f"CurseForge-Suche unterstützt nur die Loader "
                        f"{', '.join(sorted(_LOADER_TYPES))} (nicht '{loader}')"))
        params["modLoaderType"] = _LOADER_TYPES[loader]
    if (query or "").strip():
        params["searchFilter"] = query.strip()[:100]
    async with _new_client() as client:
        data = await _get_json(client, "/mods/search", params=params)
    hits = []
    for m in (data.get("data") or []):
        if not isinstance(m, dict):
            continue
        entry = _hit(m)
        loaders, versions = set(), set()
        for idx in m.get("latestFilesIndexes") or []:
            if not isinstance(idx, dict):
                continue
            ml = str(idx.get("modloader") or "").strip().lower()
            if ml in _LOADER_TYPES:
                loaders.add(ml)
            gv = str(idx.get("gameVersion") or "").strip()
            if gv:
                versions.add(gv)
        entry["loaders"] = sorted(loaders)
        entry["versions"] = sorted(versions)
        entry["compatible"] = None
        hits.append(entry)
    pagination = data.get("pagination") or {}
    return {
        "total": int(pagination.get("totalCount") or len(hits)),
        "hits": hits,
        "source": "curseforge",
    }
