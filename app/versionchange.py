"""Minecraft-Version oder Loader eines bestehenden Servers wechseln.

Ablauf (Hintergrund-Job, Server muss gestoppt sein):
1. Sicherheits-Backup der ganzen Instanz inklusive Welt ('pre-update').
2. Installierte Mods gegen die Zielversion prüfen (Modrinth, CurseForge mit
   Key): passende Versionen werden geladen, Mods ohne passende Version
   optional deaktiviert, unbekannte Dateien bleiben unverändert.
3. Version/Loader in instance.json umstellen; itzg lädt die neue
   Server-Software beim nächsten Start selbst.

Downgrades (z. B. 1.21 → 1.20) können Welten beschädigen und brauchen eine
ausdrückliche Bestätigung.
"""
import asyncio
import logging
import re

from fastapi import HTTPException

from . import instances, modrinth, runtime, updates
from .config import ALLOWED_LOADERS

logger = logging.getLogger("dashboard.versionchange")

_GAME_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z._+-]{0,31}$")
_LOADER_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_RELEASE_RE = re.compile(r"^\d+(?:\.\d+)*$")


def _version_tuple(version: str) -> tuple | None:
    if not _RELEASE_RE.match(version or ""):
        return None
    return tuple(int(part) for part in version.split("."))


def is_downgrade(current: str, target: str) -> bool:
    """True, wenn target eine ältere Release-Version ist (Snapshots und
    unbekannte Schemata gelten nicht als Downgrade)."""
    a, b = _version_tuple(current), _version_tuple(target)
    return bool(a and b and b < a)


def validate_target(loader: str, game_version: str,
                    loader_version: str | None = None) -> tuple:
    loader = (loader or "").strip().lower()
    game_version = (game_version or "").strip()
    if loader not in ALLOWED_LOADERS:
        raise HTTPException(status_code=400,
                            detail=f"Loader muss einer von {', '.join(ALLOWED_LOADERS)} sein")
    if not _GAME_VERSION_RE.match(game_version):
        raise HTTPException(status_code=400, detail="Ungültige Minecraft-Version")
    loader_version = (loader_version or "").strip() or None
    if loader_version == "auto":  # Katalog-Platzhalter für „Standard“
        loader_version = None
    if loader_version and not _LOADER_VERSION_RE.match(loader_version):
        raise HTTPException(status_code=400, detail="Ungültige Loader-Version")
    return loader, game_version, loader_version


def _has_mods(instance_id: str) -> bool:
    try:
        return any(p.name.endswith((".jar", ".jar.disabled"))
                   for p in instances.mods_dir(instance_id).iterdir())
    except OSError:
        return False


def _classify(check: dict) -> dict:
    """Mods nach Ergebnis für die Zielversion einsortieren."""
    available, missing, unknown = [], [], []
    for item in check.get("items") or []:
        brief = {"filename": item["filename"], "enabled": item["enabled"],
                 "source": item.get("source")}
        if item.get("latest"):
            installed = item.get("installed") or {}
            latest = item["latest"]
            same = (latest.get("version_id") or latest.get("file_id")) == \
                (installed.get("version_id") or installed.get("file_id"))
            available.append({**brief, "from": installed.get("version_number") or "",
                              "to": latest.get("version_number") or "",
                              "unchanged": same})
        elif item.get("no_compatible"):
            missing.append(brief)
        else:
            unknown.append(brief)
    return {"available": available, "missing": missing, "unknown": unknown}


async def check(instance_id: str, loader: str, game_version: str) -> dict:
    """Vorschau: was passiert mit den Mods beim Wechsel auf loader/game_version?"""
    instance = instances.get_instance(instance_id)
    loader, game_version, _ = validate_target(loader, game_version)
    result = {
        "current": {"loader": instance["loader"], "game_version": instance["game_version"]},
        "target": {"loader": loader, "game_version": game_version},
        "same": (loader == instance["loader"] and game_version == instance["game_version"]),
        "downgrade": is_downgrade(instance["game_version"], game_version),
        "loader_changed": loader != instance["loader"],
        "modpack": (instance.get("modpack") or {}).get("title") or None,
        "mods": {"available": [], "missing": [], "unknown": []},
    }
    if _has_mods(instance_id):
        data = await updates.check_updates(
            instance_id, target={"loader": loader, "game_version": game_version})
        result["mods"] = _classify(data)
    return result


def _ensure_stopped(instance: dict) -> None:
    running = runtime.running_state(instance)
    if running is None:
        raise HTTPException(status_code=503,
                            detail="Container-Status nicht prüfbar (Docker nicht erreichbar)")
    if running:
        raise HTTPException(status_code=409,
                            detail="Server läuft — bitte zuerst stoppen")


async def start_change(instance_id: str, loader: str, game_version: str,
                       loader_version: str | None = None, update_mods: bool = True,
                       disable_missing: bool = True, allow_downgrade: bool = False) -> dict:
    """Startet den Versionswechsel als Hintergrund-Job."""
    instance = instances.get_instance(instance_id)
    loader, game_version, loader_version = validate_target(loader, game_version, loader_version)
    if (loader == instance["loader"] and game_version == instance["game_version"]
            and loader_version == (instance.get("loader_version") or None)):
        raise HTTPException(status_code=400, detail="Server hat diese Version bereits")
    if is_downgrade(instance["game_version"], game_version) and not allow_downgrade:
        raise HTTPException(
            status_code=409,
            detail=("Ältere Minecraft-Version: Welten lassen sich nicht sicher "
                    "zurückstufen. Nur mit ausdrücklicher Bestätigung möglich."))
    await asyncio.to_thread(_ensure_stopped, instance)
    job = modrinth.create_job(f"{loader} {game_version}", 0, kind="version",
                              phase="Sicherheits-Backup", instance_id=instance_id)
    modrinth.track_task(asyncio.create_task(_run(
        job, instance_id, loader, game_version, loader_version,
        update_mods, disable_missing)))
    return job


def _fail(job: dict, message: str) -> None:
    job["status"] = "error"
    job["phase"] = "abgebrochen"
    job["error"] = message
    modrinth.persist_job(job)


async def _run(job: dict, instance_id: str, loader: str, game_version: str,
               loader_version: str | None, update_mods: bool,
               disable_missing: bool) -> None:
    from . import backups  # lazy, vermeidet Import-Zirkel
    try:
        snap = await asyncio.to_thread(
            backups.safety_backup, instance_id, instances.instance_dir(instance_id),
            "pre-update")
        job["backup"] = snap["name"]
    except Exception as exc:
        _fail(job, f"Sicherheits-Backup fehlgeschlagen, nichts geändert: {exc}")
        return

    plan: list = []
    missing: list = []
    if (update_mods or disable_missing) and _has_mods(instance_id):
        job["phase"] = "Mods prüfen"
        try:
            data = await updates.check_updates(
                instance_id, target={"loader": loader, "game_version": game_version})
        except Exception as exc:
            _fail(job, f"Mod-Prüfung fehlgeschlagen, nichts geändert: {exc}")
            return
        for item in data.get("items") or []:
            latest = item.get("latest")
            installed = item.get("installed") or {}
            if latest and update_mods:
                same = (latest.get("version_id") or latest.get("file_id")) == \
                    (installed.get("version_id") or installed.get("file_id"))
                if not same:
                    plan.append({"filename": item["filename"], "enabled": item["enabled"],
                                 "source": item["source"], "project_id": item["project_id"],
                                 "latest": latest})
            elif item.get("no_compatible") and item["enabled"]:
                missing.append(item["filename"])

    job["phase"] = "Version umstellen"
    try:
        instance = instances.get_instance(instance_id)
        old = f"{instance['loader']} {instance['game_version']}"
        instance["loader"] = loader
        instance["game_version"] = game_version
        instance["loader_version"] = loader_version
        await asyncio.to_thread(instances.update_instance, instance)
    except Exception as exc:
        _fail(job, f"Umstellen fehlgeschlagen: {exc}")
        return
    logger.info("Versionswechsel %s: %s → %s %s", instance_id, old, loader, game_version)

    disabled: list = []
    if disable_missing:
        for filename in missing:
            try:
                await asyncio.to_thread(instances.toggle_mod, instance_id, filename, False)
                disabled.append(filename)
            except Exception as exc:
                logger.warning("Mod nicht deaktivierbar (%s): %s", filename, exc)
    updates.invalidate_installed_cache(instance_id)

    if plan:
        job["total"] = len(plan)
        await updates._run_update_job(job, instance, plan, snapshot=False)
    else:
        job["results"] = []
        job["summary"] = {"updated": 0, "failed": 0, "errors": []}
        job["phase"] = "fertig"
        job["status"] = "done"
    job.setdefault("summary", {})["disabled"] = disabled
    job["summary"]["target"] = f"{loader} {game_version}"
    modrinth.persist_job(job)
