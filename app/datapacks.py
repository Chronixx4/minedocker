"""Datapacks pro Instanz: Liste, Upload (nur gestoppt), Enable/Disable und
Löschen — Dateien liegen im Welt-Ordner unter datapacks/.

Vanilla-Konvention: direkt in {world}/datapacks/ liegende .zip-Dateien sind
AKTIV; verschobene liegen in {world}/datapacks/disabled_datapacks/ (genau
so macht es der Vanilla-Befehl /datapack disable).

Bei laufender Instanz:
- disable/delete: ZUERST RCON ('/datapack disable "file/{name}"'), dann die
  Datei verschieben/löschen. RCON-Fehler = 503 und NICHTS an der Datei
  ändern (fail-closed wie die Sicherheits-Snapshots in packs.py).
- enable: Datei ZUERST nach datapacks/ verschieben (der Server scannt den
  Ordner erst dann), dann RCON '/datapack enable "file/{name}"'. RCON-
  Fehler = 503 mit Hinweis, dass das Pack beim nächsten Start lädt.
Uploads und Lösch-Aktionen ohne RCON gibt es nur bei gestoppter Instanz.
"""
import logging
import os
import re
from pathlib import Path

from fastapi import HTTPException

from . import instances

logger = logging.getLogger("dashboard.datapacks")

_PACK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.zip$")
UPLOAD_MAX_BYTES = 50 * 1024 * 1024
_DISABLED_DIR = "disabled_datapacks"
_RCON_TIMEOUT = 15.0


def validate_name(name: str) -> str:
    """Datapack-Dateiname validieren (nur .zip, keine Pfade)."""
    name = (name or "").strip()
    if not name or not _PACK_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="Ungültiger Datapack-Name: nur .zip-Dateien, 1-128 Zeichen, "
                   "Buchstaben/Zahlen/Punkt/Unterstrich/Bindestrich")
    return name


def _name(name: str) -> str:
    return validate_name(name)


def packs_dir(instance_id: str) -> Path | None:
    """Datapacks-Ordner der Welt (None = keine Welt gefunden)."""
    world = instances.world_dir(instance_id)
    return world / "datapacks" if world else None


def _ensure_dir(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f"datapacks-Ordner nicht anlegbar: {exc}")
    return path


def list_datapacks(instance_id: str) -> dict:
    """Aktive und deaktivierte Datapacks (vanilla disabled_datapacks-Ordner)."""
    base = packs_dir(instance_id)
    if base is None:
        raise HTTPException(status_code=409,
                            detail="Keine Welt gefunden — Server einmal "
                                   "starten, damit der Welt-Ordner entsteht")
    out = []
    for enabled, folder in ((True, base), (False, base / _DISABLED_DIR)):
        try:
            entries = sorted(folder.glob("*.zip"), key=lambda p: p.name.lower())
        except OSError:
            entries = []
        for path in entries:
            try:
                info = path.stat()
            except OSError:
                continue
            out.append({"name": path.name, "size": info.st_size,
                        "mtime": int(info.st_mtime), "enabled": enabled})
    return {"datapacks": out, "world": str(base.parent)}


def _resolve(base: Path, name: str, enabled: bool) -> Path:
    name = _name(name)
    folder = base if enabled else base / _DISABLED_DIR
    path = folder / name
    if not path.is_file():
        raise HTTPException(status_code=404,
                            detail=f"Datapack '{name}' nicht gefunden "
                                   f"({'aktiv' if enabled else 'deaktiviert'})")
    return path


def upload_datapack(instance_id: str, dest_name: str, data: bytes) -> dict:
    """Zip in den datapacks-Ordner schreiben (nur bei gestoppter Instanz —
    prüft der Aufrufer). Atomar via tmp + os.replace."""
    base = packs_dir(instance_id)
    if base is None:
        raise HTTPException(status_code=409,
                            detail="Keine Welt gefunden — Server einmal "
                                   "starten, damit der Welt-Ordner entsteht")
    name = _name(dest_name)
    _ensure_dir(base)
    path = base / name
    if path.exists():
        raise HTTPException(status_code=409,
                            detail=f"'{name}' existiert bereits")
    tmp = path.with_name(path.name + ".upload.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=500,
                            detail=f"Upload fehlgeschlagen: {exc}") from exc
    logger.info("Datapack hochgeladen: %s (%d Bytes) → %s", name,
                len(data), instance_id)
    return {"name": name, "size": len(data), "enabled": True}


# ---------------------------------------------------------------------------
# RCON (nur bei laufender Instanz; Fehler → HTTPException 503, fail-closed)
# ---------------------------------------------------------------------------

def _rcon_datapack(instance: dict, command: str) -> str:
    from . import rcon as rcon_mod  # lazy, vermeidet Import-Zirkel
    from . import runtime
    if not runtime.is_running(instance):
        return ""  # nichts zu tun
    host, port = runtime.rcon_target(instance)
    try:
        return rcon_mod.command(host, port, runtime.rcon_secret(instance),
                                command, timeout=_RCON_TIMEOUT)
    except (TimeoutError, rcon_mod.RconError, OSError) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"RCON nicht erreichbar ({exc}) — Aktion abgebrochen, "
                   "nichts verändert") from exc


def _rcon_pack_ref(name: str) -> str:
    return f'file/{name}'


def enable_datapack(instance_id: str, name: str) -> dict:
    """Deaktiviertes Datapack aktivieren (Datei zurück in datapacks/)."""
    instance = instances.get_instance(instance_id)
    base = packs_dir(instance_id)
    if base is None:
        raise HTTPException(status_code=409,
                            detail="Keine Welt gefunden — Server einmal "
                                   "starten, damit der Welt-Ordner entsteht")
    name = _name(name)
    dst = base / name
    if dst.exists():
        raise HTTPException(status_code=409,
                            detail=f"'{name}' existiert bereits als aktives Paket")
    src = base / _DISABLED_DIR / name
    if not src.is_file():
        raise HTTPException(status_code=404,
                            detail=f"Deaktiviertes Datapack '{name}' nicht gefunden")
    try:
        os.replace(src, dst)
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f"Aktivieren fehlgeschlagen: {exc}") from exc
    reloaded = None
    if runtime_is_running(instance):
        # Server scannt den Ordner erst jetzt — Fehler = 503 mit Hinweis
        try:
            _rcon_datapack(instance, f'datapack enable "{_rcon_pack_ref(name)}"')
            reloaded = True
        except HTTPException as exc:
            logger.warning("Datapack enable ohne RCON-Erfolg: %s (%s)",
                           name, exc.detail)
            raise HTTPException(
                status_code=503,
                detail=f"{exc.detail} — Datei liegt jetzt in datapacks/ und "
                       "lädt beim nächsten Serverstart") from exc
    logger.info("Datapack aktiviert: %s (%s, RCON=%s)", name, instance_id,
                reloaded)
    return {"name": name, "enabled": True, "reloaded": reloaded}


def disable_datapack(instance_id: str, name: str) -> dict:
    """Aktives Datapack deaktivieren (RCON zuerst, dann Datei verschieben)."""
    instance = instances.get_instance(instance_id)
    base = packs_dir(instance_id)
    if base is None:
        raise HTTPException(status_code=409,
                            detail="Keine Welt gefunden — Server einmal "
                                   "starten, damit der Welt-Ordner entsteht")
    name = _name(name)
    src = base / name
    if not src.is_file():
        raise HTTPException(status_code=404,
                            detail=f"Aktives Datapack '{name}' nicht gefunden")
    reloaded = None
    if runtime_is_running(instance):
        _rcon_datapack(instance, f'datapack disable "{_rcon_pack_ref(name)}"')
        reloaded = True
    dst_dir = base / _DISABLED_DIR
    _ensure_dir(dst_dir)
    dst = dst_dir / name
    if dst.exists():
        raise HTTPException(status_code=409,
                            detail=f"'{name}' existiert bereits als deaktiviertes "
                                   "Paket")
    try:
        os.replace(src, dst)
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f"Deaktivieren fehlgeschlagen: {exc}") from exc
    logger.info("Datapack deaktiviert: %s (%s, RCON=%s)", name, instance_id,
                reloaded)
    return {"name": name, "enabled": False, "reloaded": reloaded}


def delete_datapack(instance_id: str, name: str, enabled: bool) -> dict:
    """Datapack löschen. Bei laufender Instanz wird AKTIVEN Paketen zuerst
    per RCON entladen (fail-closed: RCON-Fehler = 503, nichts gelöscht)."""
    instance = instances.get_instance(instance_id)
    base = packs_dir(instance_id)
    if base is None:
        raise HTTPException(status_code=409,
                            detail="Keine Welt gefunden — Server einmal "
                                   "starten, damit der Welt-Ordner entsteht")
    path = _resolve(base, name, enabled)
    if enabled and runtime_is_running(instance):
        _rcon_datapack(instance, f'datapack disable "{_rcon_pack_ref(path.name)}"')
    try:
        path.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f"Löschen fehlgeschlagen: {exc}") from exc
    logger.info("Datapack gelöscht: %s (%s)", path.name, instance_id)
    return {"deleted": path.name}


def runtime_is_running(instance: dict) -> bool:
    """Laufzustand prüfen (lazy import; Fehler → nicht laufend)."""
    from . import runtime  # lazy, vermeidet Import-Zirkel
    try:
        return bool(runtime.is_running(instance))
    except Exception:
        return False


__all__ = [
    "UPLOAD_MAX_BYTES",
    "delete_datapack",
    "disable_datapack",
    "enable_datapack",
    "list_datapacks",
    "packs_dir",
    "upload_datapack",
    "validate_name",
]
