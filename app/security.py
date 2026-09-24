"""Sicherheits-Helfer: Dateinamen-Validierung, Pfadschutz, Auth-Guard."""
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request

# Erlaubte Mod-Dateinamen: enden auf .jar (".disabled" markiert deaktivierte
# Mods, die der Server ignoriert). Reale CurseForge-Dateinamen enthalten oft
# Klammern/Ausrufezeichen/Kommas/Leerzeichen — deshalb bewusst großzügig;
# Pfadtrenner, Steuerzeichen und ".." werden separat abgelehnt (Traversal).
_SAFE_NAME_RE = re.compile(r"^[^\x00-\x1f/\\]+\.(?:jar|jar\.disabled)$", re.IGNORECASE)
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
    if name.startswith("."):
        # Versteckte Dateien nicht als Mod akzeptieren (".hidden.jar")
        raise HTTPException(status_code=400, detail="Ungültiger Dateiname")
    if not _SAFE_NAME_RE.match(name):
        raise HTTPException(status_code=400,
                            detail="Nur .jar-Dateien ohne Pfad- oder "
                                   "Steuerzeichen erlaubt")
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


# Schreibende Methoden erfordern die Admin-Rolle (Viewer: strikt nur lesen —
# inkl. Konsole/Whitelist/Start-Stop). GET/HEAD/OPTIONS bleiben offen.
_WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


async def auth_guard(request: Request,
                     x_api_key: str | None = Header(default=None)) -> None:
    """Kombinierter Guard aller /api-Routen (außer /api/health und /api/auth/*):

    1. DASHBOARD_API_KEY (Header X-API-Key) bleibt voll gültig — implizit
       Rolle Admin (Skripte/curl).
    2. Session-Cookie ('mcd_session', HMAC-signiert) → Rolle aus dem Token.
    3. Ist gar nichts konfiguriert (kein API-Key, keine Benutzer), bleibt das
       Bestandsverhalten: anonymer Zugriff mit vollen Rechten.

    Rollen-Prüfung zentral hier: schreibende Methoden (POST/PATCH/PUT/DELETE)
    erfordern Admin — 403 für Viewer. GET (inkl. SSE-Log-Stream) bleibt für
    Viewer offen."""
    from . import auth as auth_mod  # lazy, vermeidet Import-Zirkel

    role = auth_mod.role_from_request(request, x_api_key)
    if role is None:
        if auth_mod.anonymous_allowed():
            role = "admin"
        else:
            raise HTTPException(
                status_code=401,
                detail="Anmeldung erforderlich (Login oder X-API-Key)")
    request.state.role = role
    if role != "admin" and request.method in _WRITE_METHODS:
        raise HTTPException(status_code=403,
                            detail="Diese Aktion erfordert die Admin-Rolle")
