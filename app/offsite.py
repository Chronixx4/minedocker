"""Off-Site-Backup: geplante Backups zusätzlich per WebDAV hochladen.

Optional über Env (Nextcloud, ZimaOS/NAS-WebDAV, Hetzner Storage Box, …):
BACKUP_WEBDAV_URL (Zielordner), BACKUP_WEBDAV_USER, BACKUP_WEBDAV_PASSWORD.
Dateien landen unter <URL>/<Instanz-ID>/<Backup-Name>; die Rotation auf der
Gegenseite übernimmt der Betreiber (das Dashboard löscht dort nichts).
"""
import logging
from pathlib import Path

import httpx

from .config import settings

logger = logging.getLogger("dashboard.offsite")


def enabled() -> bool:
    return bool(settings.backup_webdav_url)


def _auth() -> httpx.BasicAuth | None:
    if settings.backup_webdav_user:
        return httpx.BasicAuth(settings.backup_webdav_user,
                               settings.backup_webdav_password)
    return None


def upload(instance_id: str, path: Path) -> None:
    """Blockierend; wirft RuntimeError bei Fehlern (Aufrufer alarmiert)."""
    base = settings.backup_webdav_url.rstrip("/")
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, read=600.0),
                          auth=_auth()) as client:
            # Unterordner anlegen; 405/301 = existiert bereits
            client.request("MKCOL", f"{base}/{instance_id}")
            with path.open("rb") as handle:
                resp = client.put(f"{base}/{instance_id}/{path.name}", content=handle)
    except (httpx.HTTPError, OSError) as exc:
        raise RuntimeError(f"Off-Site-Upload fehlgeschlagen: {exc}") from exc
    if resp.status_code >= 300:
        raise RuntimeError(f"Off-Site-Upload fehlgeschlagen: HTTP {resp.status_code}")
    logger.info("Off-Site-Backup hochgeladen: %s/%s", instance_id, path.name)
