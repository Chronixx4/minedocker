"""Instanz-Backups: tar.gz-Snapshots je Instanz.

Speicherort: Geschwister-Ordner neben INSTANCES_DIR im selben Volume
(/data/backups/{instance_id}) — damit landen Backups NICHT im Instanz-Ordner
selbst und ein Restore kann den Instanz-Ordner ohne Sondernfälle leeren.
Hinweis: Die Snapshots liegen im selben Volume wie die Server-Daten — für
echte Off-Site-Sicherheit regelmäßig herunterladen (SERVER-SETUP.md §5).
"""
import logging
import re
import shutil
import tarfile
from datetime import datetime
from pathlib import Path

from .config import settings

logger = logging.getLogger("dashboard.backups")

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}\.tar\.gz$")

# Sicherheits-Snapshots (pre-install = vor Modpack-Installation, pre-restore =
# vor Backup-Restore). Die Rotation begrenzt sie pro Instanz, damit die
# Operationen "risikofrei" bleiben, ohne das Volume ungebremst zu füllen.
_SAFETY_KINDS = ("pre-install", "pre-restore")
_SAFETY_NAME_RE = re.compile(
    r"^(pre-install|pre-restore)-\d{8}-\d{6}(-\d+)?\.tar\.gz$")
_SAFETY_KEEP = 3

# Zeitgesteuerte Backups (Scheduler): eigenes Namensschema + eigene Rotation,
# damit sie Sicherheits-Snapshots und manuelle Backups nicht verdrängen.
_SCHEDULED_NAME_RE = re.compile(r"^scheduled-\d{8}-\d{6}(-\d+)?\.tar\.gz$")


def backups_root() -> Path:
    """Globales Backup-Verzeichnis (im mcdata-Volume)."""
    return Path(settings.instances_dir).parent / "backups"


def instance_backups_dir(instance_id: str) -> Path:
    return backups_root() / instance_id


def _safe_backup_path(instance_id: str, name: str) -> Path:
    """Anti-Path-Traversal: Name strikt validieren und Pfad-Escape prüfen."""
    if not name or not _NAME_RE.match(name) or ".." in name:
        raise ValueError("Ungültiger Backup-Name")
    bdir = instance_backups_dir(instance_id).resolve()
    candidate = (bdir / name).resolve()
    if not candidate.is_relative_to(bdir) or candidate.name != name:
        raise ValueError("Ungültiger Backup-Name")
    return candidate


def create_backup(instance_id: str, instance_dir: Path) -> dict:
    """Erzeugt einen konsistent benannten tar.gz-Snapshot der Instanz."""
    bdir = instance_backups_dir(instance_id)
    bdir.mkdir(parents=True, exist_ok=True)
    name = f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"
    dest = bdir / name
    with tarfile.open(dest, "w:gz", compresslevel=6) as tar:
        tar.add(str(instance_dir), arcname=".")
    stat = dest.stat()
    return {"name": name, "size_bytes": stat.st_size, "created": int(stat.st_mtime)}


def _safety_excludes(kind: str, instance_dir: Path) -> set:
    """Zu überspringende Top-Level-Ordner je Snapshot-Typ:
    - pre-install: Welt (bleibt unberührt) + packs (Installations-Quellen)
    - pre-restore: nur packs — Restore ersetzt ALLE Daten"""
    excludes = {"packs"}
    if kind == "pre-install":
        from .instances import find_world_dir  # lazy, vermeidet Import-Zirkel
        world = find_world_dir(instance_dir)
        if world is not None:
            excludes.add(world.name)
    return excludes


def safety_backup(instance_id: str, instance_dir: Path, kind: str) -> dict:
    """Sicherheits-Snapshot vor riskanten Operationen (Modpack-Installation /
    Restore). deckt denselben Instanz-Ordner ab, außer den ausgeschlossenen
    Top-Level-Ordnern; ältere Sicherheits-Snapshots rotieren (max _SAFETY_KEEP).
    Wirft OSError bei unbeschreibbarem Backup-Verzeichnis."""
    if kind not in _SAFETY_KINDS:
        raise ValueError(f"Unbekannter Snapshot-Typ: {kind}")
    bdir = instance_backups_dir(instance_id)
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"{kind}-{stamp}.tar.gz"
    counter = 2
    while dest.exists():  # zwei Operationen in derselben Sekunde
        dest = bdir / f"{kind}-{stamp}-{counter}.tar.gz"
        counter += 1
    excludes = _safety_excludes(kind, instance_dir)

    def _filter(info):
        name = info.name[2:] if info.name.startswith("./") else info.name
        top = name.split("/", 1)[0]
        return None if top in excludes else info

    with tarfile.open(dest, "w:gz", compresslevel=6) as tar:
        tar.add(str(instance_dir), arcname=".", filter=_filter)
    _rotate_safety(instance_id)
    stat = dest.stat()
    logger.info("Sicherheits-Snapshot: %s (%d Bytes) → %s",
                dest.name, stat.st_size, instance_id)
    return {"name": dest.name, "size_bytes": stat.st_size, "created": int(stat.st_mtime)}


def _rotate_safety(instance_id: str) -> int:
    """Löscht ältere Sicherheits-Snapshots über _SAFETY_KEEP; Rückgabe: Anzahl."""
    bdir = instance_backups_dir(instance_id)
    try:
        safety = sorted(
            (p for p in bdir.glob("*.tar.gz") if _SAFETY_NAME_RE.match(p.name)),
            key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    removed = 0
    for path in safety[_SAFETY_KEEP:]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def scheduled_backup(instance_id: str, instance_dir: Path, keep: int = 5) -> dict:
    """Zeitgesteuertes Backup (Scheduler): tar.gz-Snapshot 'scheduled-…' mit
    eigener Rotation auf max. keep Snapshots (1-20). Läuft auch bei laufender
    Instanz — für 100 % konsistente Weltschnappschüsse vorher stoppen (§5)."""
    keep = max(1, min(20, int(keep)))
    bdir = instance_backups_dir(instance_id)
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"scheduled-{stamp}.tar.gz"
    counter = 2
    while dest.exists():  # zwei Backups in derselben Sekunde
        dest = bdir / f"scheduled-{stamp}-{counter}.tar.gz"
        counter += 1
    with tarfile.open(dest, "w:gz", compresslevel=6) as tar:
        tar.add(str(instance_dir), arcname=".")
    _rotate_scheduled(instance_id, keep)
    stat = dest.stat()
    logger.info("Geplantes Backup: %s (%d Bytes) → %s",
                dest.name, stat.st_size, instance_id)
    return {"name": dest.name, "size_bytes": stat.st_size, "created": int(stat.st_mtime)}


def _rotate_scheduled(instance_id: str, keep: int) -> int:
    """Löscht ältere Scheduler-Backups über keep; Rückgabe: Anzahl."""
    bdir = instance_backups_dir(instance_id)
    try:
        scheduled = sorted(
            (p for p in bdir.glob("*.tar.gz") if _SCHEDULED_NAME_RE.match(p.name)),
            key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    removed = 0
    for path in scheduled[keep:]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def list_backups(instance_id: str) -> list:
    bdir = instance_backups_dir(instance_id)
    if not bdir.is_dir():
        return []
    out = []
    for path in sorted(bdir.glob("*.tar.gz"),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        stat = path.stat()
        out.append({"name": path.name, "size_bytes": stat.st_size,
                    "created": int(stat.st_mtime)})
    return out


def backup_path(instance_id: str, name: str) -> Path:
    path = _safe_backup_path(instance_id, name)
    if not path.is_file():
        raise FileNotFoundError("Backup nicht gefunden")
    return path


def delete_backup(instance_id: str, name: str) -> None:
    backup_path(instance_id, name).unlink()


def restore_backup(instance_id: str, name: str, instance_dir: Path) -> None:
    """Ersetzt ALLE Instanz-Daten durch den Snapshot.

    Der Instanz-Ordner wird vollständig geleert und der Inhalt des Archivs
    extrahiert (Filter 'data' neutralisiert Path-Traversal/Symlinks, PEP 706).
    Backups selbst liegen außerhalb und bleiben erhalten.
    """
    path = backup_path(instance_id, name)
    target_root = instance_dir.resolve()
    if instance_dir.exists():
        for child in instance_dir.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    instance_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            destination = (instance_dir / member.name).resolve()
            if not destination.is_relative_to(target_root):
                raise ValueError(f"Ungültiger Archiv-Pfad: {member.name}")
        tar.extractall(path=instance_dir, filter="data")
