"""Mod-Update-Prüfung: installierte Mods per Hash identifizieren und neue
kompatible Versionen auf Modrinth (SHA1) bzw. CurseForge (Murmur2-Fingerprint)
suchen — ohne die Mods vorher registriert zu haben.

Ablauf:
- Jede .jar/.jar.disabled im mods-Ordner wird einmal eingelesen; SHA1
  (Modrinth) und CurseForge-Murmur2 werden daraus berechnet.
- Modrinth: /version_file/{sha1}?multiple=true identifiziert die installierte
  Version; /project/{id}/version liefert die neueste kompatible Version.
- CurseForge: /mods/fingerprints (batchweise) identifiziert installierte
  Dateien; /mods/{id}/files liefert die neueste kompatible Datei.
- 'Alle aktualisieren' lädt je Mod die neueste kompatible Datei atomar
  (.part → rename), entfernt die alte Datei und behält den deaktiviert-Zustand.
"""
import asyncio
import hashlib
import os
import time
from pathlib import Path

import httpx
from fastapi import HTTPException

from . import instances, modrinth
from .config import settings

_MAX_CHECKED = 300       # harte Obergrenze geprüfter Dateien pro Lauf
_CF_CHUNK = 50           # Fingerprints pro CurseForge-Batch-Request
_MR_CHUNK = 40           # SHA1-Hashes pro Modrinth-Batch-Lookup
_LOOKUP_CONCURRENCY = 8  # parallele Anbieter-Abfragen im Check
_JOB_CONCURRENCY = 4     # parallele Downloads im Update-Job
_INSTALLED_TTL = 60.0    # Cache-Dauer für installierte Projekte (Such-Marker)

# Status eines geprüften Mods
S_UPDATE = "update_available"
S_CURRENT = "up_to_date"
S_NEWER = "newer_than_latest"   # installierte Version ist neuer als die 'neueste' stabile
S_UNKNOWN = "not_found"          # auf keiner Plattform identifizierbar / Hash unlesbar
S_ERROR = "error"


# ---------------------------------------------------------------------------
# Hashes
# ---------------------------------------------------------------------------

def murmur2_cf(data: bytes) -> int:
    """CurseForge-Datei-Fingerprint (Java-Referenzimplementierung, seed=1).
    Rückgabe: signed int32 (wie Java)."""
    length = len(data)
    m = 0x5BD1E995
    h = (1 ^ length) & 0xFFFFFFFF
    i = 0
    for _ in range(length // 4):
        k = (data[i] & 0xFF) | ((data[i + 1] & 0xFF) << 8) \
            | ((data[i + 2] & 0xFF) << 16) | ((data[i + 3] & 0xFF) << 24)
        k = (k * m) & 0xFFFFFFFF
        k ^= k >> 24  # Java 'k >>> r' (logisch, k ist 32-Bit-maskiert)
        k = (k * m) & 0xFFFFFFFF
        h = ((h * m) & 0xFFFFFFFF) ^ k
        i += 4
    tail = length & 3
    if tail >= 3:
        h ^= (data[(length & ~3) + 2] & 0xFF) << 16
    if tail >= 2:
        h ^= (data[(length & ~3) + 1] & 0xFF) << 8
    if tail >= 1:
        h ^= data[length & ~3] & 0xFF
        h = (h * m) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    h ^= h >> 15
    return h - 0x100000000 if h >= 0x80000000 else h


def hashes_for(path: Path) -> tuple:
    """(sha1, murmur2) einer Datei in einem Durchlauf; OSError → (None, None)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None, None
    return hashlib.sha1(data).hexdigest(), murmur2_cf(data)


# Hash-Cache je Datei: str(Pfad) → (mtime_ns, Größe, sha1, murmur2).
# Unveränderte Dateien werden nicht neu gelesen/gehasht — wichtig bei
# großen Packs (300+ Mods): sonst blockiert das Hashen die Suche bzw.
# wird bei jedem TTL-Ablauf komplett wiederholt.
_HASH_CACHE: dict = {}


def _hash_files(files) -> tuple:
    """Hasht alle Dateien (in einem Worker-Thread aufrufen!); Rückgabe
    (sha1s, murms). Unveränderte Dateien kommen aus _HASH_CACHE."""
    sha1s: list = []
    murms: list = []
    for path, _filename, _enabled in files:
        sha1, murmur = _hash_one(path)
        if sha1:
            sha1s.append(sha1)
        if murmur is not None:
            murms.append(murmur)
    return sha1s, murms


def _hash_check_files(files) -> list:
    """Hash-Daten für check_updates (in einem Worker-Thread aufrufen!),
    inkl. Nutzungs-Cache für unveränderte Dateien."""
    hashed = []
    for path, filename, enabled in files:
        sha1, murmur = _hash_one(path)
        hashed.append({"filename": filename, "enabled": enabled,
                       "size_bytes": path.stat().st_size if sha1 else None,
                       "sha1": sha1, "murmur": murmur})
    return hashed


def _hash_one(path: Path) -> tuple:
    """hashes_for mit _HASH_CACHE (mtime+Größe als Gültigkeitsprüfung)."""
    try:
        st = path.stat()
    except OSError:
        return None, None
    key = str(path)
    cached = _HASH_CACHE.get(key)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2], cached[3]
    sha1, murmur = hashes_for(path)
    if sha1:
        _HASH_CACHE[key] = (st.st_mtime_ns, st.st_size, sha1, murmur)
    return sha1, murmur


# ---------------------------------------------------------------------------
# Modrinth-Identifikation
# ---------------------------------------------------------------------------

def _cf_available() -> bool:
    """CurseForge-Prüfung nur mit gesetztem CF_API_KEY."""
    return bool(settings.cf_api_key)


async def _modrinth_lookup(client: httpx.AsyncClient, sha1: str,
                           loader: str, game_version: str) -> dict | None:
    """Installierte Version per SHA1 identifizieren → Version-Objekt oder None."""
    try:
        resp = await client.get(
            f"{settings.modrinth_api}/version_file/{sha1}",
            params={"multiple": "true"}, headers=modrinth._headers())
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        versions = resp.json()
    except ValueError:
        return None
    if not isinstance(versions, list) or not versions:
        return None

    def fits(v):
        return game_version in (v.get("game_versions") or []) \
            and loader in (v.get("loaders") or [])

    return next((v for v in versions if fits(v)), versions[0])


async def _modrinth_latest(client: httpx.AsyncClient, project_id: str,
                           loader: str, game_version: str) -> dict | None:
    """Neueste kompatible Version eines Modrinth-Projekts (neueste zuerst)."""
    try:
        resp = await client.get(
            f"{settings.modrinth_api}/project/{project_id}/version",
            params={
                "loaders": f'["{loader}"]',
                "game_versions": f'["{game_version}"]',
            }, headers=modrinth._headers())
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        versions = resp.json()
    except ValueError:
        return None
    return versions[0] if isinstance(versions, list) and versions else None


def _mr_file(version: dict) -> dict | None:
    """Primäre Datei eines Modrinth-Version-Objekts."""
    files = version.get("files") or []
    return next((f for f in files if f.get("primary")), None) \
        or (files[0] if files else None)


# ---------------------------------------------------------------------------
# CurseForge-Identifikation (Fingerprint-Batch)
# ---------------------------------------------------------------------------

def _cf_headers() -> dict:
    from . import curseforge
    return curseforge._headers()


async def _cf_fingerprint_map(client: httpx.AsyncClient,
                              murmur_hashes: list) -> dict:
    """CurseForge-Fingerprints batchweise auflösen → {murmur: Datei-Objekt}."""
    out = {}
    for start in range(0, len(murmur_hashes), _CF_CHUNK):
        chunk = murmur_hashes[start:start + _CF_CHUNK]
        try:
            resp = await client.post(
                f"{settings.curseforge_api}/mods/fingerprints",
                json={"fileFingerprints": chunk},
                headers=_cf_headers())
        except httpx.HTTPError:
            continue
        if resp.status_code != 200:
            continue
        try:
            data = resp.json().get("data") or {}
        except ValueError:
            continue
        matches = data.get("exactMatches") or []
        exact_fps = data.get("exactFingerprints")
        for idx, match in enumerate(matches):
            if not isinstance(match, dict) or not isinstance(match.get("file"), dict):
                continue
            fp = exact_fps[idx] if isinstance(exact_fps, list) and idx < len(exact_fps) \
                else None
            if fp is None and len(matches) == 1 and len(chunk) == 1:
                fp = chunk[0]  # Eindeutig: einziger Treffer des einzigen Werts
            if fp is not None:
                out[fp] = match["file"]
    return out


async def _cf_files(client: httpx.AsyncClient, mod_id: str,
                    game_version: str, loader: str) -> list:
    """Dateien eines CF-Mods, gefiltert nach MC-Version + Loader-Typ."""
    from . import curseforge
    params: dict[str, str | int] = {"pageSize": 100}
    if game_version:
        params["gameVersion"] = game_version
    if loader in curseforge._LOADER_TYPES:
        params["modLoaderType"] = curseforge._LOADER_TYPES[loader]
    try:
        resp = await client.get(f"{settings.curseforge_api}/mods/{mod_id}/files",
                                params=params, headers=_cf_headers())
    except httpx.HTTPError:
        return []
    if resp.status_code != 200:
        return []
    try:
        return resp.json().get("data") or []
    except ValueError:
        return []


# ---------------------------------------------------------------------------
# Update-Check
# ---------------------------------------------------------------------------

def _iter_mod_files(instance_id: str) -> list:
    """Alle Mods (aktiv + deaktiviert) als [(pfad, dateiname, enabled)]."""
    directory = instances.mods_dir(instance_id)
    return [(directory / mod["filename"], mod["filename"], mod["enabled"])
            for mod in instances.list_mods(instance_id)]


def _compare(installed_date: str, latest_date: str) -> str:
    """Downgrade-Schutz: 'neueste' ist nur ein Update, wenn sie nicht älter ist."""
    if not installed_date or not latest_date:
        return S_UPDATE
    return S_UPDATE if str(latest_date) >= str(installed_date) else S_NEWER


# Cache: Instanz-ID → (Zeitstempel, Ergebnis), damit die Mod-Suche nicht bei
# jedem Tastendruck alle jars neu hashen muss (Mod-Install/Edit invalidiert
# innerhalb der TTL von selbst).
_INSTALLED_CACHE: dict = {}
# Laufende Hintergrund-Auffrischungen (stale-while-revalidate)
_REFRESHING: set = set()


def invalidate_installed_cache(instance_id: str | None = None) -> None:
    """Leert den Installiert-Cache (gesamt oder je Instanz)."""
    if instance_id is None:
        _INSTALLED_CACHE.clear()
    else:
        _INSTALLED_CACHE.pop(instance_id, None)


def _refresh_installed_async(instance_id: str) -> None:
    """Auffrischen im Hintergrund (stale-while-revalidate); wirft nie."""
    if instance_id in _REFRESHING:
        return
    _REFRESHING.add(instance_id)

    async def _run():
        try:
            await _installed_project_ids_uncached(instance_id)
        except Exception:
            pass  # Hintergrund-Auffrischung darf nichts brechen
        finally:
            _REFRESHING.discard(instance_id)

    try:
        task = asyncio.create_task(_run())
        task.add_done_callback(lambda _t: _REFRESHING.discard(instance_id))
    except RuntimeError:  # kein laufender Loop (z. B. in Tests)
        _REFRESHING.discard(instance_id)


async def installed_project_ids(instance_id: str) -> dict:
    """Identifiziert installierte Mods der Instanz per Datei-Hash.
    Rückgabe: {'modrinth': {projekt_id}, 'curseforge': {mod_id}, 'checked': n}.
    Wirft nicht — Anbieter-Fehler liefern leere Mengen; Ergebnis wird
    _INSTALLED_TTL Sekunden gecacht. Nach TTL-Ablauf kommen sofort die
    letzten bekannten Daten zurück (stale-while-revalidate), die Auffrischung
    läuft im Hintergrund — die Suche wartet nie auf das Hashen."""
    now = time.monotonic()
    cached = _INSTALLED_CACHE.get(instance_id)
    if cached is not None and now - cached[0] <= _INSTALLED_TTL:
        return cached[1]
    if cached is not None:
        _refresh_installed_async(instance_id)
        return cached[1]
    return await _installed_project_ids_uncached(instance_id)


async def _installed_project_ids_uncached(instance_id: str) -> dict:
    now = time.monotonic()
    out: dict = {"modrinth": set(), "curseforge": set(), "checked": 0}
    try:
        files = _iter_mod_files(instance_id)
    except HTTPException:
        _INSTALLED_CACHE[instance_id] = (now, out)
        return out
    files = files[:_MAX_CHECKED]
    out["checked"] = len(files)
    # Hashing in einen Worker-Thread auslagern: mehrere GB Jar-Daten im
    # Event-Loop blockierten sonst das GESAMTE Dashboard (Suche, Katalog,
    # Status-Polls — Symptom: „Suche dauert ewig / Katalog nicht erreichbar“).
    sha1s, murms = await asyncio.to_thread(_hash_files, files)
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Modrinth: Batch-Lookup der SHA1s → Versionen (oder null) → Projekt-IDs
        for start in range(0, len(sha1s), _MR_CHUNK):
            chunk = sha1s[start:start + _MR_CHUNK]
            try:
                resp = await client.get(
                    f"{settings.modrinth_api}/version_file/{','.join(chunk)}",
                    params={"multiple": "true"}, headers=modrinth._headers())
            except httpx.HTTPError:
                continue
            if resp.status_code != 200:
                continue
            try:
                versions = resp.json()
            except ValueError:
                continue
            if not isinstance(versions, list):
                continue
            for version in versions:
                if isinstance(version, dict) and version.get("project_id"):
                    out["modrinth"].add(str(version["project_id"]))
        # CurseForge: Fingerprints batchweise → Mod-IDs (nur mit CF_API_KEY)
        if _cf_available() and murms:
            cf_map = await _cf_fingerprint_map(client, murms)
            for file in cf_map.values():
                mod_id = file.get("modId")
                if mod_id:
                    out["curseforge"].add(str(mod_id))
    _INSTALLED_CACHE[instance_id] = (now, out)
    return out


async def check_updates(instance_id: str) -> dict:
    """Prüft alle Mods der Instanz gegen Modrinth (+ CurseForge mit Key).
    Wirft HTTPException nur für 404/400; Anbieter-Ausfälle pro Mod toleriert."""
    instance = instances.get_instance(instance_id)
    loader = instance["loader"]
    game_version = instance["game_version"]
    files = _iter_mod_files(instance_id)
    if len(files) > _MAX_CHECKED:
        raise HTTPException(status_code=400,
                            detail=f"Zu viele Mods (max. {_MAX_CHECKED} prüfbar)")

    hashed = await asyncio.to_thread(_hash_check_files, files)

    use_cf = _cf_available() and any(h["murmur"] is not None for h in hashed)
    async with httpx.AsyncClient(timeout=15.0) as client:
        cf_map = {}
        if use_cf:
            cf_map = await _cf_fingerprint_map(
                client, [h["murmur"] for h in hashed if h["murmur"] is not None])

        sem = asyncio.Semaphore(_LOOKUP_CONCURRENCY)

        async def identify(item):
            """Modrinth-SHA1-Lookup, Fallback CurseForge-Fingerprint."""
            async with sem:
                if not item["sha1"]:
                    return None
                installed = await _modrinth_lookup(client, item["sha1"],
                                                   loader, game_version)
                if installed is not None:
                    return {"source": "modrinth", "installed": installed}
                cf_file = cf_map.get(item["murmur"]) if item["murmur"] is not None \
                    else None
                if cf_file is not None:
                    return {"source": "curseforge", "installed_file": cf_file}
                return None

        found = await asyncio.gather(*(identify(i) for i in hashed))

        async def latest_for(item, info):
            """Neueste kompatible Version der identifizierten Quelle."""
            async with sem:
                if info["source"] == "modrinth":
                    return await _modrinth_latest(client, info["installed"]
                                                  .get("project_id") or "",
                                                  loader, game_version)
                cf_files = await _cf_files(client, str(info["installed_file"]
                                                       .get("modId") or ""),
                                           game_version, loader)
                from . import curseforge
                try:
                    return curseforge._pick_file(cf_files, jar_preferred=True)
                except HTTPException:
                    return None  # keine Dateien mehr verfügbar

        latest_results = await asyncio.gather(
            *(latest_for(i, f) if f else _none() for i, f in zip(hashed, found,
                                                                 strict=False)))

    items = []
    for item, info, latest in zip(hashed, found, latest_results, strict=False):
        base = {
            "filename": item["filename"], "enabled": item["enabled"],
            "size_bytes": item["size_bytes"], "status": S_UNKNOWN,
            "source": None, "project_id": None,
            "installed": None, "latest": None,
        }
        if info is None or latest is None:
            items.append(base)
            continue
        if info["source"] == "modrinth":
            installed = info["installed"]
            file = _mr_file(latest) or {}
            base.update(
                source="modrinth", project_id=installed.get("project_id") or "",
                installed={
                    "version_id": installed.get("id") or "",
                    "version_number": installed.get("version_number") or "",
                    "date": installed.get("date_published") or "",
                },
                latest={
                    "version_id": latest.get("id") or "",
                    "version_number": latest.get("version_number") or "",
                    "filename": file.get("filename") or "",
                    "size": int(file.get("size") or 0),
                    "date": latest.get("date_published") or "",
                },
            )
            base["status"] = (S_CURRENT if base["latest"]["version_id"] ==
                              base["installed"]["version_id"]
                              else _compare(base["installed"]["date"],
                                            base["latest"]["date"]))
        else:
            cf_file = info["installed_file"]
            mod_id = str(cf_file.get("modId") or "")
            base.update(
                source="curseforge", project_id=mod_id,
                installed={
                    "file_id": str(cf_file.get("id") or ""),
                    "version_number": str(cf_file.get("displayName")
                                          or cf_file.get("fileName") or ""),
                    "date": str(cf_file.get("fileDate") or ""),
                },
                latest={
                    "file_id": str(latest.get("id") or ""),
                    "version_number": str(latest.get("displayName")
                                          or latest.get("fileName") or ""),
                    "filename": str(latest.get("fileName") or ""),
                    "size": int(latest.get("fileSize") or 0),
                    "date": str(latest.get("fileDate") or ""),
                },
            )
            base["status"] = (S_CURRENT if base["latest"]["file_id"] ==
                              base["installed"]["file_id"]
                              else _compare(base["installed"]["date"],
                                            base["latest"]["date"]))
        items.append(base)

    return {"items": items, "checked": len(items),
            "updatable": sum(1 for i in items if i["status"] == S_UPDATE),
            "curseforge_enabled": _cf_available()}


async def _none():
    return None


# ---------------------------------------------------------------------------
# Update-Job ("Alle aktualisieren")
# ---------------------------------------------------------------------------

def _plan_from_check(check: dict, filenames: list | None) -> list:
    """Update-Plan aus dem Check-Ergebnis: nur Mods mit Status update_available
    (optional gefiltert auf übergebene Dateinamen)."""
    wanted = {f.lower() for f in (filenames or [])} or None
    plan = []
    for item in check.get("items") or []:
        if item.get("status") != S_UPDATE or not item.get("latest"):
            continue
        if wanted and item["filename"].lower() not in wanted:
            continue
        plan.append({
            "filename": item["filename"],
            "enabled": item["enabled"],
            "source": item["source"],
            "project_id": item["project_id"],
            "latest": item["latest"],
        })
    return plan


async def _download_to(client: httpx.AsyncClient, url: str, dest: Path,
                       expected_size: int, sha1=None,
                       headers: dict | None = None,
                       verify_url: bool = False) -> None:
    """Datei atomar herunterladen (.part → rename), optional SHA1-Prüfung
    und CurseForge-CDN-Host-Prüfung (Anti-SSRF, wie bei Einzel-Downloads)."""
    from .security import validate_curseforge_download_url
    part = dest.with_suffix(dest.suffix + ".part")
    digest = hashlib.sha1() if sha1 else None
    try:
        async with client.stream("GET", url, headers=headers or {},
                                 follow_redirects=True) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code} bei Download")
            if verify_url:
                validate_curseforge_download_url(str(resp.url))
            with open(part, "wb") as fh:
                async for chunk in resp.aiter_bytes(65536):
                    fh.write(chunk)
                    if digest:
                        digest.update(chunk)
        if expected_size and abs(part.stat().st_size - expected_size) > 65536:
            raise RuntimeError("Download unvollständig")
        if digest and part.stat().st_size < 64 * 1024 * 1024 \
                and digest.hexdigest() != sha1:
            raise RuntimeError("SHA1-Prüfung fehlgeschlagen")
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)


async def _resolve_latest(plan_item: dict, instance: dict,
                          client: httpx.AsyncClient) -> dict:
    """Neueste Datei zum Update-Zeitpunkt frisch auflösen
    → {filename, url, size, sha1, date}."""
    loader, game_version = instance["loader"], instance["game_version"]
    if plan_item["source"] == "curseforge":
        from . import curseforge
        cf_files = await _cf_files(client, plan_item["project_id"],
                                   game_version, loader)
        chosen = curseforge._pick_file(cf_files, jar_preferred=True)
        if chosen is None:
            raise RuntimeError("Keine CurseForge-Datei gefunden")
        return {
            "filename": str(chosen.get("fileName")
                            or plan_item["latest"]["filename"]),
            "url": curseforge._file_url(chosen, int(plan_item["project_id"])),
            "size": int(chosen.get("fileSize") or 0),
            "sha1": (chosen.get("hashes") or {}).get("sha1"),
            "date": str(chosen.get("fileDate") or ""),
        }
    version = await _modrinth_latest(client, plan_item["project_id"],
                                     loader, game_version)
    if version is None:
        raise RuntimeError("Keine kompatible Version gefunden")
    file = _mr_file(version)
    if not file or not file.get("url"):
        raise RuntimeError("Keine .jar-Datei gefunden")
    return {
        "filename": str(file.get("filename") or ""),
        "url": str(file["url"]),
        "size": int(file.get("size") or 0),
        "sha1": (file.get("hashes") or {}).get("sha1"),
        "date": str(version.get("date_published") or ""),
    }


async def _run_update_job(job: dict, instance: dict, plan: list) -> None:
    """Führt den Update-Plan aus: je Mod die neueste Datei laden und ersetzen.
    Fehler einzelner Mods brechen den Job nicht ab (Ergebnisliste)."""
    from . import curseforge
    mods_root = instances.mods_dir(instance["id"])
    sem = asyncio.Semaphore(_JOB_CONCURRENCY)

    async def one(item):
        async with sem:
            old_name = item["filename"]
            was_disabled = not item["enabled"]
            entry = {"filename": old_name, "status": "updated"}
            try:
                latest = await _resolve_latest(item, instance, client)
                target_name = latest["filename"] or old_name
                if was_disabled and not target_name.endswith(".disabled"):
                    target_name += ".disabled"
                old_path = mods_root / old_name
                new_path = mods_root / target_name
                if new_path.resolve() != old_path.resolve() and new_path.exists():
                    raise RuntimeError(f"Zieldatei '{target_name}' existiert bereits")
                headers = curseforge._headers() \
                    if item["source"] == "curseforge" else modrinth._headers()
                await _download_to(
                    client, latest["url"], new_path, latest["size"],
                    sha1=latest["sha1"], headers=headers,
                    verify_url=item["source"] == "curseforge")
                if new_path.resolve() != old_path.resolve() and old_path.is_file():
                    old_path.unlink()
                entry["new_filename"] = target_name
            except Exception as exc:
                entry["status"] = S_ERROR
                entry["error"] = str(exc) or exc.__class__.__name__
            job["downloaded"] += 1
            job["phase"] = f"Aktualisiere ({job['downloaded']}/{job['total']})"
            job["results"].append(entry)

    job["results"] = []
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0),
                               follow_redirects=True)
    try:
        await asyncio.gather(*(one(i) for i in plan))
    finally:
        await client.aclose()
        invalidate_installed_cache(instance["id"])  # Dateien haben sich geändert
    failed = [r for r in job["results"] if r["status"] == S_ERROR]
    job["phase"] = "fertig"
    job["summary"] = {
        "updated": len(job["results"]) - len(failed),
        "failed": len(failed),
        "errors": [f"{r['filename']}: {r.get('error')}" for r in failed],
    }
    job["status"] = "done"
    modrinth.persist_job(job)


async def start_update(instance_id: str, filenames: list | None = None) -> dict:
    """'Alles aktualisieren': Update-Check ausführen und passende Dateien
    im Hintergrund ersetzen. Rückgabe: Job (kind='update')."""
    instance = instances.get_instance(instance_id)
    check = await check_updates(instance_id)
    plan = _plan_from_check(check, filenames)
    if not plan:
        raise HTTPException(
            status_code=409,
            detail="Keine Mods mit verfügbarem Update (zuerst prüfen lassen)")
    job = modrinth.create_job("Mods aktualisieren", len(plan), kind="update",
                              phase=f"Aktualisiere (0/{len(plan)})",
                              instance_id=instance_id)
    modrinth.track_task(asyncio.create_task(_run_update_job(job, instance, plan)))
    return job


__all__ = [
    "S_CURRENT",
    "S_NEWER",
    "S_UNKNOWN",
    "S_UPDATE",
    "check_updates",
    "hashes_for",
    "installed_project_ids",
    "invalidate_installed_cache",
    "murmur2_cf",
    "start_update",
]
