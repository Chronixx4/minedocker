"""Modrinth-API-Client (v2) und Download-Job-Verwaltung mit Fortschritt."""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

import httpx
from fastapi import HTTPException

from .config import settings
from .security import validate_curseforge_download_url

logger = logging.getLogger("dashboard.modrinth")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# In-Memory-Jobs (leichtgewichtig, Maximalbegrenzung gegen Speicherwucher).
# Zusätzlich wird jeder Job-Zustand als JSON-Datei gespiegelt (Überleben von
# Dashboard-Restarts): restore_jobs() lädt sie beim Start, aktive Jobs werden
# als abgebrochen markiert; snapshot_jobs() spiegelt regelmäßig (Flush-Task).
JOBS: dict = {}
_JOB_ORDER: list = []
_running_tasks: set = set()
MAX_JOBS = 20

# Status, die einen noch laufenden Vorgang kennzeichnen (werden bei einem
# Neustart als abgebrochen markiert)
_ACTIVE_STATUSES = ("downloading", "installing")
_JOBS_KEEP_DAYS = 7


def jobs_dir() -> Path:
    """Spiegel-Ordner für Job-Zustände (im mcdata-Volume neben den Instanzen)."""
    return settings.instances_dir.parent / "jobs"


def persist_job(job: dict) -> None:
    """Spiegelt einen Job-Zustand atomar auf Platte (wirft nie)."""
    path = jobs_dir() / f"{job.get('id', 'unknown')}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(job), encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("Job-Spiegel nicht schreibbar (%s): %s",
                       path.name, exc)


def _delete_persisted(job_id: str) -> None:
    try:
        (jobs_dir() / f"{job_id}.json").unlink(missing_ok=True)
    except OSError:
        pass


def restore_jobs() -> int:
    """Liest gespiegelte Jobs beim Start wieder ein (neueste zuerst bleiben,
    max MAX_JOBS); Jobs, die im Moment des Neustarts aktiv waren, werden als
    'Abgebrochen (Neustart)' markiert. Rückgabe: Anzahl wiederhergestellter
    Jobs. Wirft nicht — ein defekter Spiegel wird verworfen."""
    directory = jobs_dir()
    cutoff = time.time() - _JOBS_KEEP_DAYS * 86400
    try:
        files = [p for p in directory.glob("*.json") if p.stat().st_mtime >= cutoff]
    except OSError:
        return 0
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)  # neueste zuerst
    restored = 0
    for path in reversed(files):  # älteste zuerst einlesen → neueste bleiben
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        if not isinstance(data, dict) or not data.get("id") \
                or str(data["id"]) in JOBS:
            continue
        if data.get("status") in _ACTIVE_STATUSES:
            data["status"] = "error"
            data["phase"] = "Abgebrochen (Neustart)"
            data["error"] = "Dashboard-Neustart — laufender Vorgang abgebrochen"
        job_id = str(data["id"])
        JOBS[job_id] = data
        _JOB_ORDER.append(job_id)
        restored += 1
    while len(_JOB_ORDER) > MAX_JOBS:
        evicted = _JOB_ORDER.pop(0)
        JOBS.pop(evicted, None)
        _delete_persisted(evicted)
    for job in list(JOBS.values()):
        persist_job(job)  # abgebrochene Zustände sofort spiegeln
    return restored


def snapshot_jobs() -> int:
    """Spiegelt alle aktuellen Jobs auf Platte und entfernt verwaiste/
    abgelaufene Spiegel-Dateien. Rückgabe: Anzahl gespiegelter Jobs."""
    for job in list(JOBS.values()):
        persist_job(job)
    directory = jobs_dir()
    try:
        entries = list(directory.glob("*.json"))
    except OSError:
        return len(JOBS)
    cutoff = time.time() - _JOBS_KEEP_DAYS * 86400
    for path in entries:
        if path.stem in JOBS:
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            pass
    return len(JOBS)


def _headers() -> dict:
    return {"User-Agent": settings.user_agent, "Accept": "application/json"}


def _validate_id(value: str, what: str) -> str:
    if not value or not _ID_RE.match(value):
        raise HTTPException(status_code=400, detail=f"Ungültiger Wert für {what}")
    return value


def track_task(task: "asyncio.Task") -> None:
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)


# Sortierung: Frontend-Key -> Modrinth-Suchindex
_SORT_INDEX = {
    "relevance": "relevance",
    "downloads": "downloads",
    "updated": "updated",
    "newest": "newest",
}


# Umgebungsfilter: welche Seite den Mod braucht bzw. wo er zwingend nötig ist.
# Modrinth-Feldwerte: required, optional, unsupported, unknown.
_ENVIRONMENT_FACETS = {
    "server_required": ["server_side:required"],
    "server": ["server_side:required", "server_side:optional"],
    "client_required": ["client_side:required"],
    "client": ["client_side:required", "client_side:optional"],
}


async def search_mods(query: str, loader: str | None, game_version: str | None,
                      limit: int = 20, offset: int = 0, sort: str = "relevance",
                      environment: str | None = None) -> dict:
    """Sucht Mods, optional gefiltert nach Loader/MC-Version/Umgebung.

    loader=None bzw. game_version=None lässt den jeweiligen Filter weg
    (Treffer aller Loader bzw. aller MC-Versionen)."""
    if sort not in _SORT_INDEX:
        raise HTTPException(status_code=400,
                            detail=f"Sortierung muss einer von {', '.join(_SORT_INDEX)} sein")
    if environment and environment not in _ENVIRONMENT_FACETS:
        raise HTTPException(status_code=400,
                            detail=f"Umgebung muss einer von "
                                   f"{', '.join(_ENVIRONMENT_FACETS)} sein")
    facets = [["project_type:mod"]]
    if loader:
        facets.append([f"categories:{loader}"])
    if game_version:
        facets.append([f"versions:{game_version}"])
    if environment:
        facets.append(_ENVIRONMENT_FACETS[environment])
    params: dict[str, str | int] = {
        "limit": limit,
        "offset": offset,
        "index": _SORT_INDEX[sort],
        "facets": json.dumps(facets),
    }
    if query:
        params["query"] = query
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
    hits = [
        {
            "project_id": h.get("project_id") or "",
            "slug": h.get("slug") or "",
            "title": h.get("title") or "",
            "author": h.get("author") or "",
            "description": h.get("description") or "",
            "downloads": int(h.get("downloads") or 0),
            "icon_url": h.get("icon_url") or "",
            "latest_version": h.get("latest_version") or "",
            "server_side": h.get("server_side") or "unknown",
        }
        for h in data.get("hits", [])
    ]
    return {
        # Modrinth nennt das Feld total_hits; 'total' ist ein Fallback
        "total": int(data.get("total_hits") or data.get("total") or len(hits)),
        "hits": hits,
        "loader": loader,
        "game_version": game_version,
    }


async def _get_json(client: httpx.AsyncClient, path: str, params: dict | None = None):
    try:
        resp = await client.get(f"{settings.modrinth_api}{path}",
                                params=params or {}, headers=_headers())
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502,
                            detail=f"Modrinth nicht erreichbar ({exc.__class__.__name__})")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Projekt/Version auf Modrinth nicht gefunden")
    if resp.status_code != 200:
        raise HTTPException(status_code=502,
                            detail=f"Modrinth antwortete mit HTTP {resp.status_code}")
    return resp.json()


def _pick_file(version: dict) -> tuple[str, str, int]:
    files = version.get("files") or []
    primary = next((f for f in files if f.get("primary")), None)
    if primary is None:
        jars = [f for f in files if str(f.get("filename") or "").lower().endswith(".jar")]
        primary = max(jars, key=lambda f: f.get("size") or 0) if jars else None
    if not primary or not primary.get("url"):
        raise HTTPException(status_code=404, detail="Keine passende .jar-Datei gefunden")
    return str(primary["filename"]), str(primary["url"]), int(primary.get("size") or 0)


def _pick_file_full(version: dict) -> tuple[str, str, int, str | None]:
    """Wie _pick_file, zusätzlich SHA1 der Datei (falls Anbieter liefert)."""
    filename, url, size = _pick_file(version)
    files = version.get("files") or []
    primary = next((f for f in files if f.get("primary")), None) \
        or (files[0] if files else None)
    sha1 = None
    if isinstance(primary, dict):
        sha1 = (primary.get("hashes") or {}).get("sha1") or None
    return filename, url, size, sha1


# Abhängigkeits-Auflösung (Modrinth): Tiefe 2 reicht praktisch (Mod → API →
# Basis-Bibliothek); Obergrenze gegen ausufernde Ketten.
_MAX_DEPS = 20
_DEP_DEPTH = 2


async def _version_for_project(client: httpx.AsyncClient, project_id: str,
                               loader: str, game_version: str) -> dict | None:
    """Neueste kompatible Version eines Projekts oder None (Deps sind
    best effort — fehlt ein Provider-Treffer, wird die Dep übersprungen)."""
    try:
        versions = await _get_json(
            client, f"/project/{project_id}/version",
            params={"loaders": json.dumps([loader]),
                    "game_versions": json.dumps([game_version])})
    except HTTPException:
        return None
    return versions[0] if versions else None


async def dependency_files(project_id: str, version_id: str | None, loader: str,
                           game_version: str,
                           installed_project_ids: set | None = None) -> dict:
    """Löst die Pflicht-Abhängigkeiten (dependency_type='required') eines
    Modrinth-Mods zu Download-Einträgen auf — rekursiv bis Tiefe 2, max
    _MAX_DEPS Dateien. Bereits installierte Projekte und Zyklen werden
    übersprungen. Rückgabe: {'files': [{filename, url, size, sha1,
    project_id, version_id}], 'skipped': [{project_id, reason}]}."""
    _validate_id(project_id, "Projekt-ID")
    async with httpx.AsyncClient(timeout=15.0) as client:
        if version_id:
            _validate_id(version_id, "Versions-ID")
            main_version = await _get_json(client, f"/version/{version_id}")
        else:
            versions = await _get_json(
                client, f"/project/{project_id}/version",
                params={"loaders": json.dumps([loader]),
                        "game_versions": json.dumps([game_version])})
            if not versions:
                return {"files": [], "skipped": []}
            main_version = versions[0]
        visited = {str(main_version.get("project_id") or project_id)}
        files: list = []
        skipped: list = []
        queue = [(main_version, 0)]
        while queue and len(files) < _MAX_DEPS:
            version, depth = queue.pop(0)
            if depth >= _DEP_DEPTH:
                continue
            for dep in version.get("dependencies") or []:
                if not isinstance(dep, dict):
                    continue
                if dep.get("dependency_type") != "required":
                    continue  # optional/embedded/incompatible ignorieren
                dep_id = str(dep.get("project_id") or "")
                if not dep_id or dep_id in visited:
                    continue
                visited.add(dep_id)
                if installed_project_ids and dep_id in installed_project_ids:
                    skipped.append({"project_id": dep_id,
                                    "reason": "bereits installiert"})
                    continue
                if len(files) >= _MAX_DEPS:
                    skipped.append({"project_id": dep_id,
                                    "reason": "Limit erreicht"})
                    continue
                dep_version = await _version_for_project(
                    client, dep_id, loader, game_version)
                if dep_version is None:
                    skipped.append({"project_id": dep_id,
                                    "reason": "keine kompatible Version"})
                    continue
                try:
                    filename, url, size, sha1 = _pick_file_full(dep_version)
                except HTTPException:
                    skipped.append({"project_id": dep_id,
                                    "reason": "keine .jar-Datei"})
                    continue
                files.append({"filename": filename, "url": url, "size": size,
                              "sha1": sha1, "project_id": dep_id,
                              "version_id": dep_version.get("id") or ""})
                queue.append((dep_version, depth + 1))
        return {"files": files, "skipped": skipped}


async def resolve_download(project_id: str, version_id: str | None,
                           loader: str, game_version: str) -> tuple[str, str, int]:
    """Löst ein Projekt zur neuesten kompatiblen Datei auf:
    (Dateiname, Download-URL, Größe in Bytes)."""
    _validate_id(project_id, "Projekt-ID")
    async with httpx.AsyncClient(timeout=15.0) as client:
        if version_id:
            _validate_id(version_id, "Versions-ID")
            version = await _get_json(client, f"/version/{version_id}")
        else:
            versions = await _get_json(
                client,
                f"/project/{project_id}/version",
                params={
                    "loaders": json.dumps([loader]),
                    "game_versions": json.dumps([game_version]),
                },
            )
            if not versions:
                raise HTTPException(
                    status_code=404,
                    detail=f"Keine Version kompatibel mit {loader} {game_version}",
                )
            version = versions[0]  # Modrinth liefert neueste zuerst
    return _pick_file(version)


def create_job(filename: str, total: int, kind: str = "mod", **extra) -> dict:
    job = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "filename": filename,
        "status": "downloading",
        "phase": None,
        "downloaded": 0,
        "total": total,
        "error": None,
        "started": int(time.time()),
    }
    job.update(extra)
    JOBS[job["id"]] = job
    _JOB_ORDER.append(job["id"])
    while len(_JOB_ORDER) > MAX_JOBS:
        evicted = _JOB_ORDER.pop(0)
        JOBS.pop(evicted, None)
        _delete_persisted(evicted)
    persist_job(job)
    return job


def get_job(job_id: str) -> dict | None:
    return JOBS.get(job_id)


async def _download_one(job: dict, url: str, dest: Path, sha1=None,
                        verify_url: bool = False, expected: int = 0) -> bool:
    """Ein Datei-Download: atomar (.part-Tempdatei + rename), optionale
    SHA1-Prüfung; verify_url begrenzt den Ziel-Host auf die CurseForge-CDN-
    Allowlist (Anti-SSRF). Zählt auf job['downloaded'] auf; bei Problemen
    Fehlermeldung am Job hinterlegen und False zurückgeben (wirft nie)."""
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        timeout = httpx.Timeout(30.0, read=120.0)
        received = 0
        digest = hashlib.sha1() if sha1 else None
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client, \
                client.stream("GET", url, headers=_headers()) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"Download-Server antwortete mit HTTP {resp.status_code}")
            if verify_url:
                validate_curseforge_download_url(str(resp.url))
            with open(part, "wb") as fh:
                async for chunk in resp.aiter_bytes(65536):
                    fh.write(chunk)
                    received += len(chunk)
                    job["downloaded"] += len(chunk)
                    if digest:
                        digest.update(chunk)
        expected = expected or int(job["total"] or 0)
        if expected and received and abs(received - expected) > 65536:
            raise RuntimeError("Download unvollständig")
        if (digest and part.stat().st_size < 64 * 1024 * 1024
                and digest.hexdigest() != sha1):
            raise RuntimeError("SHA1-Prüfung fehlgeschlagen")
        os.replace(part, dest)
        return True
    except Exception as exc:
        if not job.get("error"):
            job["error"] = str(exc) or exc.__class__.__name__
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        return False


async def run_download_job(job: dict, url: str, dest: Path,
                           sha1=None, verify_url: bool = False) -> None:
    """Streamt eine Datei atomar in den mods-Ordner und setzt den Job-Status."""
    try:
        if await _download_one(job, url, dest, sha1=sha1, verify_url=verify_url):
            job["status"] = "done"
        else:
            job["status"] = "error"
            job["error"] = job.get("error") or "Download fehlgeschlagen"
    finally:
        persist_job(job)
        try:  # Installiert-Cache auffrischen (falls Instanz-Ziel)
            from . import updates
            updates.invalidate_installed_cache(job.get("instance_id"))
        except Exception:
            pass


async def run_bundle_job(job: dict, dest_dir: Path, verify_url: bool = False) -> None:
    """Lädt alle Einträge eines Mod-Bundles (job['bundle'], Hauptdatei zuerst)
    nacheinander atomar in dest_dir. Fehler an Abhängigkeiten sammeln sich in
    job['dep_errors'] (Job bleibt 'done'); ein Fehler an der Hauptdatei bricht
    den Job mit 'error' ab. Bereits vorhandene Dep-Dateien werden übersprungen."""
    from .security import safe_mods_path
    entries = job.get("bundle") or []
    dep_errors: list = []
    try:
        for idx, entry in enumerate(entries):
            if job["status"] == "error":
                break  # Hauptdatei fehlgeschlagen → Rest abbrechen
            kind = entry.get("kind") or "dep"
            dest = safe_mods_path(dest_dir, str(entry.get("filename") or ""))
            if dest.exists() and kind != "main":
                entry["status"] = "übersprungen (existiert bereits)"
                continue
            job["phase"] = f"Datei {idx + 1}/{len(entries)}: {entry.get('filename')}"
            if await _download_one(job, str(entry.get("url") or ""), dest,
                                   sha1=entry.get("sha1"), verify_url=verify_url,
                                   expected=int(entry.get("size") or 0)):
                entry["status"] = "fertig"
            elif kind == "main":
                entry["status"] = "fehler"
                job["status"] = "error"
            else:
                entry["status"] = "fehler"
                dep_errors.append(str(entry.get("filename")))
        if job["status"] != "error":
            job["status"] = "done"
            job["phase"] = "Fertig"
            if dep_errors:
                job["dep_errors"] = dep_errors
    finally:
        persist_job(job)
        try:  # Installiert-Cache auffrischen (Mod-Menge hat sich geändert)
            from . import updates
            instance_id = job.get("instance_id")
            updates.invalidate_installed_cache(instance_id)
        except Exception:
            pass
