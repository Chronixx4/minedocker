"""Server-Icon je Instanz (server-icon.png).

Minecraft liest server-icon.png aus dem Server-Ordner und zeigt es in der
Serverliste. Das Modul validiert PNG strikt mit der Standardbibliothek:
Magic-Bytes \x89PNG\r\n\x1a\n und exakt 64x64 Pixel (Minecraft skaliert
nicht — jede andere Größe erscheint leer oder verzerrt). Der Austausch ist
auch bei laufender Instanz erlaubt, das Icon wird beim Ping gelesen, nicht
beim Start.
"""
import logging
import os
import struct
from pathlib import Path

from fastapi import HTTPException

from . import instances

logger = logging.getLogger("dashboard.icon")

ICON_NAME = "server-icon.png"
ICON_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB — reichlich für ein 64x64-PNG

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def icon_path(instance_id: str) -> Path:
    """Pfad der Icon-Datei im Instanz-Ordner."""
    return instances.instance_dir(instance_id) / ICON_NAME


def validate_png_64(content: bytes) -> None:
    """PNG-Prüfung (Magic + IHDR 64x64) — HTTPException 400 bei Verstößen."""
    if not isinstance(content, bytes) or len(content) < 24:
        raise HTTPException(status_code=400, detail="Keine gültige PNG-Datei")
    if content[:8] != _PNG_MAGIC:
        raise HTTPException(status_code=400,
                            detail="Nur PNG-Dateien werden akzeptiert")
    try:
        # IHDR: Länge (4 B, immer 13), Typ 'IHDR' (4 B), dann Breite/Höhe je
        # 4 B big-endian an Offset 16/20
        chunk_len, chunk_type = struct.unpack(">I4s", content[8:16])
        if chunk_type != b"IHDR" or chunk_len < 8:
            raise ValueError
        width, height = struct.unpack(">II", content[16:24])
    except (ValueError, struct.error):
        raise HTTPException(status_code=400,
                            detail="Keine gültige PNG-Datei (IHDR defekt)") from None
    if (width, height) != (64, 64):
        raise HTTPException(
            status_code=400,
            detail=f"Server-Icon muss exakt 64x64 Pixel sein "
                   f"(geliefert: {width}x{height})")


def icon_exists(instance_id: str) -> bool:
    return icon_path(instance_id).is_file()


def icon_info(instance_id: str) -> dict:
    """(exists, size_bytes, path) für GET-Listen/Detail-Antworten."""
    path = icon_path(instance_id)
    try:
        size = path.stat().st_size
    except OSError:
        return {"exists": False, "size_bytes": 0, "path": str(path)}
    return {"exists": True, "size_bytes": size, "path": str(path)}


def _instance_name(instance_id: str) -> str:
    try:
        return str(instances.get_instance(instance_id).get("name") or instance_id)
    except HTTPException:
        return instance_id


def write_icon(instance_id: str, content: bytes) -> dict:
    """server-icon.png atomar ersetzen (auch bei laufender Instanz)."""
    instances.get_instance(instance_id)  # 404 für unbekannte Instanzen
    validate_png_64(content)
    if len(content) > ICON_MAX_BYTES:
        raise HTTPException(status_code=413,
                            detail="Icon zu groß (max 1 MiB)")
    path = icon_path(instance_id)
    tmp = path.with_name(ICON_NAME + ".tmp")
    try:
        tmp.write_bytes(content)
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=500,
                            detail=f"Icon nicht schreibbar: {exc}") from exc
    logger.info("Server-Icon gesetzt: %s", _instance_name(instance_id))
    return icon_info(instance_id)


def delete_icon(instance_id: str) -> dict:
    """server-icon.png entfernen (404 wenn fehlt)."""
    instances.get_instance(instance_id)  # 404 für unbekannte Instanzen
    path = icon_path(instance_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Kein Server-Icon vorhanden")
    try:
        path.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f"Icon nicht löschbar: {exc}") from exc
    logger.info("Server-Icon entfernt: %s", _instance_name(instance_id))
    return {"deleted": ICON_NAME}
