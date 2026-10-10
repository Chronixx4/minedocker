"""Modbrowser: Projekt-Details mit Versionsliste und gebündelte Installation.

- project_details(): Beschreibung, Links und die Versionen eines Modrinth-
  oder CurseForge-Projekts, gefiltert auf Loader und MC-Version der Instanz
  (oder ungefiltert).
- build_plan(): löst eine oder mehrere vorgemerkte Mods (optional mit fester
  Version) samt Pflicht-Abhängigkeiten auf; Abhängigkeiten werden über alle
  Mods gemeinsam aufgelöst und nur einmal geladen.
- start_install(): startet aus einem Plan einen einzigen Download-Job.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx
from fastapi import HTTPException

from . import curseforge, instances, modrinth, updates
from .security import safe_mods_path

logger = logging.getLogger("dashboard.modinstall")

SOURCES = ("modrinth", "curseforge")
MAX_ITEMS = 25
_MAX_VERSIONS = 50
_MAX_CHANGELOG = 6000
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<\s*(br|/p|/li|/h\d)\s*/?>", re.IGNORECASE)


def _html_to_text(html: str) -> str:
    """CurseForge-Changelogs sind HTML; für die Anzeige als Text reicht das."""
    import html as html_mod
    text = _BR_RE.sub("\n", html or "")
    text = _TAG_RE.sub("", text)
    text = html_mod.unescape(text)
    lines = [line.rstrip() for line in text.splitlines()]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _cut(text: str | None, limit: int = _MAX_CHANGELOG) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


# ---------------------------------------------------------------------------
# Projekt-Details + Versionen
# ---------------------------------------------------------------------------

async def _modrinth_details(project_id: str, loader: str, game_version: str,
                            all_versions: bool) -> dict:
    modrinth._validate_id(project_id, "Projekt-ID")
    params = {} if all_versions else {
        "loaders": json.dumps([loader]), "game_versions": json.dumps([game_version])}
    async with httpx.AsyncClient(timeout=15.0) as client:
        project, versions = await asyncio.gather(
            modrinth._get_json(client, f"/project/{project_id}"),
            modrinth._get_json(client, f"/project/{project_id}/version", params=params))
    out_versions = []
    for v in (versions or [])[:_MAX_VERSIONS]:
        if not isinstance(v, dict):
            continue
        try:
            filename, _url, size = modrinth._pick_file(v)
        except HTTPException:
            continue
        out_versions.append({
            "id": str(v.get("id") or ""),
            "version_number": str(v.get("version_number") or ""),
            "name": str(v.get("name") or ""),
            "type": str(v.get("version_type") or "release"),
            "date": str(v.get("date_published") or ""),
            "game_versions": [str(g) for g in (v.get("game_versions") or [])][-6:],
            "loaders": [str(x) for x in (v.get("loaders") or [])],
            "filename": filename,
            "size": size,
            "changelog": _cut(v.get("changelog")),
            "required_dependencies": sum(
                1 for d in (v.get("dependencies") or [])
                if isinstance(d, dict) and d.get("dependency_type") == "required"),
        })
    slug = str(project.get("slug") or project_id)
    return {
        "source": "modrinth",
        "project": {
            "project_id": str(project.get("id") or project_id),
            "title": str(project.get("title") or slug),
            "description": str(project.get("description") or ""),
            "icon_url": str(project.get("icon_url") or ""),
            "page_url": f"https://modrinth.com/mod/{slug}",
            "downloads": int(project.get("downloads") or 0),
            "categories": [str(c) for c in (project.get("categories") or [])][:8],
            "server_side": project.get("server_side") or "unknown",
            "client_side": project.get("client_side") or "unknown",
        },
        "versions": out_versions,
    }


_CF_RELEASE = {1: "release", 2: "beta", 3: "alpha"}


async def _curseforge_details(project_id: str, loader: str, game_version: str,
                              all_versions: bool) -> dict:
    curseforge._require_key()
    async with curseforge._new_client() as client:
        mod = await curseforge._resolve_project(client, project_id)
        mod_id = int(mod.get("id") or 0)
        params = {"pageSize": _MAX_VERSIONS} if all_versions \
            else {**curseforge._files_params(loader, game_version), "pageSize": _MAX_VERSIONS}
        data = await curseforge._get_json(client, f"/mods/{mod_id}/files", params=params)
    files = (data.get("data") or []) if isinstance(data, dict) else []
    files = [f for f in files if isinstance(f, dict) and f.get("fileName")]
    files.sort(key=lambda f: str(f.get("fileDate") or ""), reverse=True)
    out_versions = []
    for f in files[:_MAX_VERSIONS]:
        versions, loaders = curseforge.file_version_tags(f)
        out_versions.append({
            "id": str(f.get("id") or ""),
            "version_number": str(f.get("displayName") or f.get("fileName")),
            "name": str(f.get("displayName") or ""),
            "type": _CF_RELEASE.get(int(f.get("releaseType") or 1), "release"),
            "date": str(f.get("fileDate") or ""),
            "game_versions": versions[-6:],
            "loaders": loaders,
            "filename": str(f.get("fileName")),
            "size": int(f.get("fileLength") or f.get("fileSize") or 0),
            "changelog": None,  # bei CF nur per Einzelabruf (changelog())
            "required_dependencies": len(curseforge.required_dep_ids(f)),
        })
    links = mod.get("links") or {}
    logo = mod.get("logo") or {}
    return {
        "source": "curseforge",
        "project": {
            "project_id": str(mod_id),
            "title": str(mod.get("name") or project_id),
            "description": str(mod.get("summary") or ""),
            "icon_url": str(logo.get("thumbnailUrl") or logo.get("url") or ""),
            "page_url": str(links.get("websiteUrl") or ""),
            "downloads": int(mod.get("downloadCount") or 0),
            "categories": [str(c.get("name")) for c in (mod.get("categories") or [])
                           if isinstance(c, dict) and c.get("name")][:8],
            "server_side": None,
            "client_side": None,
        },
        "versions": out_versions,
    }


async def project_details(instance: dict, source: str, project_id: str,
                          all_versions: bool = False) -> dict:
    loader = instance["loader"]
    game_version = instance["game_version"]
    if source == "modrinth":
        data = await _modrinth_details(project_id, loader, game_version, all_versions)
    elif source == "curseforge":
        data = await _curseforge_details(project_id, loader, game_version, all_versions)
    else:
        raise HTTPException(status_code=400, detail="Quelle muss modrinth oder curseforge sein")
    data["filtered"] = not all_versions
    data["loader"] = loader
    data["game_version"] = game_version
    return data


async def curseforge_changelog(project_id: str, file_id: str) -> str:
    curseforge._require_key()
    curseforge._validate_id(project_id, "Projekt-ID")
    if not file_id.isdigit() or len(file_id) > 12:
        raise HTTPException(status_code=400, detail="Ungültige Datei-ID")
    async with curseforge._new_client() as client:
        mod = await curseforge._resolve_project(client, project_id)
        data = await curseforge._get_json(
            client, f"/mods/{int(mod.get('id') or 0)}/files/{file_id}/changelog")
    raw = data.get("data") if isinstance(data, dict) else ""
    return _cut(_html_to_text(str(raw or "")))


# ---------------------------------------------------------------------------
# Plan + Installation
# ---------------------------------------------------------------------------

async def _resolve_modrinth_item(project_id: str, version_id: str | None,
                                 loader: str, game_version: str) -> dict:
    modrinth._validate_id(project_id, "Projekt-ID")
    async with httpx.AsyncClient(timeout=15.0) as client:
        if version_id:
            modrinth._validate_id(version_id, "Versions-ID")
            version = await modrinth._get_json(client, f"/version/{version_id}")
            owner = str(version.get("project_id") or "")
            if owner and owner != project_id:
                project = await modrinth._get_json(client, f"/project/{project_id}")
                if str(project.get("id") or "") != owner:
                    raise HTTPException(status_code=400,
                                        detail="Version gehört nicht zu diesem Projekt")
        else:
            versions = await modrinth._get_json(
                client, f"/project/{project_id}/version",
                params={"loaders": json.dumps([loader]),
                        "game_versions": json.dumps([game_version])})
            if not versions:
                raise HTTPException(
                    status_code=404,
                    detail=f"Keine Version kompatibel mit {loader} {game_version}")
            version = versions[0]
    filename, url, size, sha1 = modrinth._pick_file_full(version)
    return {"source": "modrinth", "project_id": str(version.get("project_id") or project_id),
            "version_id": str(version.get("id") or ""),
            "version_number": str(version.get("version_number") or ""),
            "filename": filename, "url": url, "size": size, "sha1": sha1}


async def build_plan(instance: dict, items: list[dict]) -> dict:
    """Plan für die Installation: Hauptdateien, gemeinsame Abhängigkeiten,
    übersprungene Abhängigkeiten, Fehler je Mod und Namenskonflikte."""
    if not items:
        raise HTTPException(status_code=400, detail="Keine Mods ausgewählt")
    if len(items) > MAX_ITEMS:
        raise HTTPException(status_code=400,
                            detail=f"Höchstens {MAX_ITEMS} Mods auf einmal")
    loader = instance["loader"]
    game_version = instance["game_version"]
    target_dir = instances.mods_dir(instance["id"])
    try:
        installed = await updates.installed_project_ids(instance["id"])
    except Exception as exc:  # best effort, wie bei der Einzelinstallation
        logger.warning("Installiert-Erkennung nicht möglich: %s", exc)
        installed = {"modrinth": set(), "curseforge": set()}
    known = {src: set(installed.get(src) or set()) | {
        str(i["project_id"]) for i in items if i["source"] == src} for src in SOURCES}

    mains: list[dict] = []
    deps: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    seen_files: set[str] = set()
    seen_deps: set[tuple[str, str]] = set()

    for item in items:
        source = item["source"]
        project_id = str(item["project_id"])
        version_id = item.get("version_id") or None
        try:
            if source == "modrinth":
                main = await _resolve_modrinth_item(project_id, version_id, loader,
                                                    game_version)
                bundle = await modrinth.dependency_files(
                    project_id, main["version_id"], loader, game_version, known["modrinth"])
            else:
                filename, url, size, sha1, dep_ids = await curseforge.resolve_download_full(
                    project_id, version_id, loader, game_version)
                main = {"source": "curseforge", "project_id": project_id,
                        "version_id": version_id or "", "version_number": "",
                        "filename": filename, "url": url, "size": size, "sha1": sha1}
                bundle = await curseforge.dependency_files(
                    dep_ids, loader, game_version, known["curseforge"])
        except HTTPException as exc:
            errors.append({"source": source, "project_id": project_id,
                           "detail": str(exc.detail)})
            continue
        except Exception as exc:
            logger.exception("Auflösung fehlgeschlagen (%s %s)", source, project_id)
            errors.append({"source": source, "project_id": project_id,
                           "detail": f"{source} nicht erreichbar: {exc}"})
            continue
        main["title"] = str(item.get("title") or "")[:100]
        if main["filename"].lower() in seen_files:
            continue  # zweimal dieselbe Datei vorgemerkt
        seen_files.add(main["filename"].lower())
        mains.append(main)
        for entry in bundle.get("files") or []:
            key = (source, str(entry.get("project_id") or ""))
            name = str(entry.get("filename") or "")
            if key in seen_deps or name.lower() in seen_files:
                continue
            seen_deps.add(key)
            known[source].add(key[1])
            if safe_mods_path(target_dir, name).exists():
                skipped.append({"filename": name, "project_id": key[1],
                                "reason": "Datei existiert bereits"})
                continue
            seen_files.add(name.lower())
            deps.append({**entry, "source": source})
        skipped.extend(bundle.get("skipped") or [])

    conflicts = [m["filename"] for m in mains
                 if safe_mods_path(target_dir, m["filename"]).exists()]
    return {
        "items": mains,
        "dependencies": deps,
        "skipped": skipped,
        "errors": errors,
        "conflicts": conflicts,
        "total_size": sum(int(e.get("size") or 0) for e in mains + deps),
    }


class InstallConflict(Exception):
    """Hauptdateien liegen schon im mods-Ordner (ohne overwrite → 409)."""

    def __init__(self, conflicts: list[str]):
        super().__init__(", ".join(conflicts))
        self.conflicts = conflicts


def public_plan(plan: dict) -> dict:
    """Plan ohne Download-URLs/Hashes für die Oberfläche."""
    def strip(entry: dict) -> dict:
        return {k: v for k, v in entry.items() if k not in ("url", "sha1")}
    return {**plan, "items": [strip(e) for e in plan["items"]],
            "dependencies": [strip(e) for e in plan["dependencies"]]}


async def start_install(instance: dict, items: list[dict], overwrite: bool) -> dict:
    plan = await build_plan(instance, items)
    if not plan["items"]:
        detail = "; ".join(e["detail"] for e in plan["errors"]) or "Nichts zu installieren"
        raise HTTPException(status_code=400, detail=detail)
    if plan["conflicts"] and not overwrite:
        raise InstallConflict(plan["conflicts"])
    target_dir = instances.mods_dir(instance["id"])
    target_dir.mkdir(parents=True, exist_ok=True)
    # Überschriebene Dateien vorher in den Papierkorb kopieren (Rückweg)
    for name in plan["conflicts"]:
        path = safe_mods_path(target_dir, name)
        if path.is_file():
            await asyncio.to_thread(instances.move_to_trash, instance["id"], path,
                                    "update", True)
    entries = []
    for e in plan["items"]:
        entries.append({**e, "kind": "item", "verify_url": e["source"] == "curseforge"})
    for e in plan["dependencies"]:
        entries.append({**e, "kind": "dep", "verify_url": e["source"] == "curseforge"})
    total = sum(int(e.get("size") or 0) for e in entries)
    label = plan["items"][0]["filename"] if len(plan["items"]) == 1 \
        else f"{len(plan['items'])} Mods"
    job = modrinth.create_job(label, total, kind="mod", source="bundle",
                              instance_id=instance["id"])
    job["bundle"] = entries
    job["items"] = [{"filename": e["filename"], "project_id": e["project_id"],
                     "source": e["source"]} for e in plan["items"]]
    job["dependencies"] = [{"filename": e["filename"], "project_id": e.get("project_id")}
                           for e in plan["dependencies"]]
    if plan["skipped"]:
        job["skipped"] = plan["skipped"]
    modrinth.persist_job(job)
    modrinth.track_task(asyncio.create_task(modrinth.run_bundle_job(job, target_dir)))
    logger.info("Mod-Installation gestartet: %s → %s (%d Datei(en))",
                label, instance["id"], len(entries))
    return {"job_id": job["id"], "total": total,
            "items": job["items"], "dependencies": job["dependencies"],
            "skipped": plan["skipped"], "errors": plan["errors"]}
