"""Sicherheits-Helfer: Dateinamen-Validierung, Pfadschutz, API-Key."""
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Header, HTTPException

# Erlaubte Mod-Dateinamen: einfache Zeichen, muss auf .jar enden;
# ".disabled" markiert deaktivierte Mods (werden vom Server ignoriert)
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-()+\[\]]*\.jar(\.disabled)?$")
# IDs/Versionen für Modrinth- und Versionsangaben
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# Anti-SSRF: CurseForge-Downloads dürfen nur von diesen Hosts kommen
_CF_DL_HOSTS = ("www.curseforge.com",)
_CF_DL_HOST_SUFFIXES = (".forgecdn.net",)


def validate_curseforge_download_url(url: str) -> str:
    """Prüft, dass eine Download-URL (auch nach Redirect) auf einem
    CurseForge-CDN-Host per HTTPS liegt. Löst RuntimeError bei Verstößen."""
    parts = urlsplit(str(url))
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (
            host in _CF_DL_HOSTS
            or any(host.endswith(sfx) for sfx in _CF_DL_HOST_SUFFIXES)):
        raise RuntimeError(f"Download-URL mit nicht erlaubtem Host: {host or '?'}")
    return str(url)


def validate_filename(name: str) -> str:
    if not name or len(name) > 255:
        raise HTTPException(status_code=400, detail="Ungültiger Dateiname")
    if "/" in name or "\\" in name or ".." in name or "\x00" in name:
        raise HTTPException(status_code=400, detail="Ungültiger Dateiname")
    if not _SAFE_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Nur .jar-Dateien mit einfachen Namen erlaubt")
    return name


def validate_identifier(value: str, what: str) -> str:
    if not value or not _ID_RE.match(value):
        raise HTTPException(status_code=400, detail=f"Ungültiger Wert für {what}")
    return value


def safe_mods_path(mods_dir: Path, filename: str) -> Path:
    """Löst einen Dateinamen innerhalb des mods-Ordners auf und verhindert
    Pfadmanipulation (Traversal) durch Doppelprüfung mit resolve()."""
    validate_filename(filename)
    base = mods_dir.resolve()
    path = (base / filename).resolve()
    if path.parent != base:
        raise HTTPException(status_code=400, detail="Pfadmanipulation erkannt")
    return path


async def api_key_guard(x_api_key: str | None = Header(default=None)) -> None:
    """Optionaler Schutz aller /api-Routen: Ist DASHBOARD_API_KEY gesetzt,
    muss der Header X-API-Key übereinstimmen."""
    from .config import settings  # lazy, vermeidet Import-Zirkel

    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Ungültiger oder fehlender API-Key")
