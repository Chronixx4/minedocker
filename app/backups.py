"""Instanz-Backups: tar.gz-Snapshots je Instanz.

Speicherort: BACKUPS_DIR/{instance_id}, standardmäßig der Geschwister-Ordner
neben INSTANCES_DIR im selben Volume (/data/backups) — damit landen Backups
NICHT im Instanz-Ordner selbst und ein Restore kann den Instanz-Ordner ohne
Sondernfälle leeren. Mit BACKUPS_DIR auf einer zweiten Platte überstehen sie
auch einen Ausfall der Server-Platte (SERVER-SETUP.md §5).
"""
import contextlib
import json
import logging
import os
import re
import shutil
import tarfile
import threading
from datetime import datetime
from pathlib import Path

from . import auth
from .config import settings

logger = logging.getLogger("dashboard.backups")

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}\.tar\.gz$")

# Sicherheits-Snapshots (pre-install = vor Modpack-Installation, pre-restore =
# vor Backup-Restore, pre-update = vor Mod-Updates und Versionswechsel). Die Rotation begrenzt sie pro Instanz, damit die
# Operationen "risikofrei" bleiben, ohne das Volume ungebremst zu füllen.
_SAFETY_KINDS = ("pre-install", "pre-restore", "pre-update")
_SAFETY_NAME_RE = re.compile(
    r"^(pre-install|pre-restore|pre-update)-\d{8}-\d{6}(-\d+)?\.tar\.gz$")
_SAFETY_KEEP = 3

# Zeitgesteuerte Backups (Scheduler): eigenes Namensschema + eigene Rotation,
# damit sie Sicherheits-Snapshots und manuelle Backups nicht verdrängen.
_SCHEDULED_NAME_RE = re.compile(r"^scheduled-\d{8}-\d{6}(-\d+)?\.tar\.gz$")
_MANUAL_NAME_RE = re.compile(r"^backup-\d{8}-\d{6}(-\d+)?\.tar\.gz$")


def backups_root() -> Path:
    """Globales Backup-Verzeichnis: BACKUPS_DIR (z. B. zweite Platte) oder
    standardmäßig neben den Instanzen im selben Volume."""
    if settings.backups_dir:
        return settings.backups_dir
    return Path(settings.instances_dir).parent / "backups"


def is_external() -> bool:
    """True, wenn die Backups auf einem anderen Dateisystem liegen als die
    Server (übersteht dann einen Ausfall der Server-Platte)."""
    root = backups_root()
    try:
        probe = root if root.exists() else root.parent
        return probe.stat().st_dev != Path(settings.instances_dir).stat().st_dev
    except OSError:
        return False


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


# Pfade, die nie ins Backup gehören: alles, was jederzeit neu erzeugt wird
# (BlueMap-Renderdaten, Logs, Crash-Reports, Caches, Remapping-Ordner) oder
# bereits anderswo liegt (alte Backups, Welt-Backups, AutoModpack-Hostcontent
# = Kopie der Mods). Relative Pfade ab Instanz-Ordner; ein Eintrag schließt
# den ganzen Unterbaum aus.
_ALWAYS_EXCLUDE = {
    "bluemap", "logs", "crash-reports", "cache", ".cache", ".fabric",
    "debug", "backups", "world-backups", ".paper-remapped",
    "automodpack/host-modpack",
}

_SETTINGS_LOCK = threading.Lock()
SETTINGS_DEFAULTS = {
    "compress_level": 6,   # gzip 1 (schnell) .. 9 (klein)
    "safety_keep": _SAFETY_KEEP,  # Sicherheits-Snapshots je Typ
    "manual_keep": 0,      # manuelle Backups; 0 = nie automatisch löschen
    "extra_excludes": [],  # zusätzliche Pfade (z. B. "dynmap")
}
_EXCLUDE_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")


def settings_path() -> Path:
    return auth.users_path().parent / "backup_settings.json"


def _clamp(value, lo, hi, default):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _clean_excludes(raw) -> list:
    out = []
    for item in raw if isinstance(raw, list) else []:
        item = str(item).strip().strip("/")
        if item.startswith("./"):
            item = item[2:]
        if item and ".." not in item.split("/") and _EXCLUDE_RE.match(item) \
                and item not in out:
            out.append(item)
    return out[:50]


def load_settings() -> dict:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    d = SETTINGS_DEFAULTS
    return {
        "compress_level": _clamp(data.get("compress_level"), 1, 9, d["compress_level"]),
        "safety_keep": _clamp(data.get("safety_keep"), 1, 10, d["safety_keep"]),
        "manual_keep": _clamp(data.get("manual_keep"), 0, 50, d["manual_keep"]),
        "extra_excludes": _clean_excludes(data.get("extra_excludes")),
    }


def save_settings(**changes) -> dict:
    """Teil-Update der Backup-Einstellungen (None = unverändert)."""
    with _SETTINGS_LOCK:
        cfg = load_settings()
        for key, value in changes.items():
            if value is not None and key in cfg:
                cfg[key] = value
        cfg = {**cfg, "extra_excludes": _clean_excludes(cfg["extra_excludes"])}
        cfg["compress_level"] = _clamp(cfg["compress_level"], 1, 9, 6)
        cfg["safety_keep"] = _clamp(cfg["safety_keep"], 1, 10, _SAFETY_KEEP)
        cfg["manual_keep"] = _clamp(cfg["manual_keep"], 0, 50, 0)
        path = settings_path()
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle)
        os.replace(tmp, path)
    return cfg


def exclude_set(cfg: dict | None = None) -> set:
    cfg = cfg or load_settings()
    return set(_ALWAYS_EXCLUDE) | set(cfg["extra_excludes"])


def _top_filter(excludes: set):
    """tarfile-Filter: lässt Einträge weg, deren Pfad (oder ein Elternpfad)
    in excludes liegt."""
    def _filter(info):
        name = info.name[2:] if info.name.startswith("./") else info.name
        parts = name.split("/")
        for i in range(1, len(parts) + 1):
            if "/".join(parts[:i]) in excludes:
                return None
        return info
    return _filter


def usage() -> dict:
    """Speicherverbrauch aller Backups (je Instanz) + freier Platz der Ablage."""
    root = backups_root()
    per = {}
    total = 0
    if root.is_dir():
        for sub in root.iterdir():
            if not sub.is_dir():
                continue
            size = count = 0
            for f in sub.glob("*.tar.gz"):
                try:
                    size += f.stat().st_size
                    count += 1
                except OSError:
                    pass
            per[sub.name] = {"bytes": size, "count": count}
            total += size
    try:
        probe = root if root.exists() else root.parent
        free = shutil.disk_usage(probe).free
    except OSError:
        free = None
    return {"total_bytes": total, "free_bytes": free, "instances": per}


def _rcon_quiet(instance: dict, command: str) -> bool:
    from . import rcon as rcon_mod  # lazy, vermeidet Import-Zirkel
    from . import runtime
    try:
        host, port = runtime.rcon_target(instance)
        rcon_mod.command(host, port, runtime.rcon_secret(instance), command,
                         timeout=60.0)
        return True
    except Exception as exc:  # best effort: Backup läuft trotzdem
        logger.warning("RCON '%s' fehlgeschlagen (%s): %s",
                       command, instance.get("name"), exc)
        return False


@contextlib.contextmanager
def world_flushed(instance_id: str):
    """Bei laufendem Server: Auto-Save aus + alles auf die Platte schreiben
    (save-off, save-all flush), danach Auto-Save wieder an. So landen keine
    halb geschriebenen Region-Dateien im Backup. Gestoppt: nichts zu tun."""
    from . import instances, runtime  # lazy, vermeidet Import-Zirkel
    try:
        instance = instances.get_instance(instance_id)
        running = runtime.is_running(instance)
    except Exception:
        running = False
    # Schlafmodus: Welt wurde vor dem Einfrieren gespeichert, der Prozess
    # schreibt nichts — RCON würde den Server nur unnötig wecken
    if running and instances.is_paused(instance_id):
        running = False
    paused = False
    if running:
        paused = _rcon_quiet(instance, "save-off")
        if paused:
            _rcon_quiet(instance, "save-all flush")
    try:
        yield
    finally:
        if paused:
            _rcon_quiet(instance, "save-on")


def create_backup(instance_id: str, instance_dir: Path) -> dict:
    """Erzeugt einen konsistent benannten tar.gz-Snapshot der Instanz."""
    bdir = instance_backups_dir(instance_id)
    bdir.mkdir(parents=True, exist_ok=True)
    name = f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"
    dest = bdir / name
    cfg = load_settings()
    with world_flushed(instance_id), tarfile.open(
            dest, "w:gz", compresslevel=cfg["compress_level"]) as tar:
        tar.add(str(instance_dir), arcname=".", filter=_top_filter(exclude_set(cfg)))
    if cfg["manual_keep"]:
        _rotate_matching(instance_id, _MANUAL_NAME_RE, cfg["manual_keep"])
    stat = dest.stat()
    return {"name": name, "size_bytes": stat.st_size, "created": int(stat.st_mtime)}


def _safety_excludes(kind: str, instance_dir: Path) -> set:
    """Zu überspringende Top-Level-Ordner je Snapshot-Typ:
    - pre-install: Welt (bleibt unberührt) + packs (Installations-Quellen)
    - pre-restore/pre-update: nur packs (Restore ersetzt ALLE Daten, neue
      Mod-/Spielversionen können die Welt beim ersten Start umschreiben)"""
    excludes = {"packs", *exclude_set()}
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
    level = load_settings()["compress_level"]
    with world_flushed(instance_id), tarfile.open(dest, "w:gz", compresslevel=level) as tar:
        tar.add(str(instance_dir), arcname=".", filter=_top_filter(excludes))
    _rotate_safety(instance_id)
    stat = dest.stat()
    logger.info("Sicherheits-Snapshot: %s (%d Bytes) → %s",
                dest.name, stat.st_size, instance_id)
    return {"name": dest.name, "size_bytes": stat.st_size, "created": int(stat.st_mtime)}


def _rotate_matching(instance_id: str, name_re, keep: int) -> int:
    """Löscht ältere Backups, deren Name name_re trifft, über keep hinaus."""
    bdir = instance_backups_dir(instance_id)
    try:
        items = sorted(
            (p for p in bdir.glob("*.tar.gz") if name_re.match(p.name)),
            key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    removed = 0
    for path in items[keep:]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _rotate_safety(instance_id: str) -> int:
    """Rotiert Sicherheits-Snapshots (alle Typen zusammen) auf die Einstellung
    safety_keep; Rückgabe: Anzahl gelöschter Dateien."""
    return _rotate_matching(instance_id, _SAFETY_NAME_RE,
                            load_settings()["safety_keep"])


def scheduled_backup(instance_id: str, instance_dir: Path, keep: int = 5) -> dict:
    """Zeitgesteuertes Backup (Scheduler): tar.gz-Snapshot 'scheduled-…' mit
    eigener Rotation auf max. keep Snapshots (1-20). Läuft auch bei laufender
    Instanz (vorher save-off + save-all flush per RCON, danach save-on)."""
    keep = max(1, min(20, int(keep)))
    bdir = instance_backups_dir(instance_id)
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"scheduled-{stamp}.tar.gz"
    counter = 2
    while dest.exists():  # zwei Backups in derselben Sekunde
        dest = bdir / f"scheduled-{stamp}-{counter}.tar.gz"
        counter += 1
    cfg = load_settings()
    with world_flushed(instance_id), tarfile.open(
            dest, "w:gz", compresslevel=cfg["compress_level"]) as tar:
        tar.add(str(instance_dir), arcname=".", filter=_top_filter(exclude_set(cfg)))
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
