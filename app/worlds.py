"""Welt-Verwaltung + Server-Import: Zip-Download des Welt-Ordners,
Welt-Upload (.zip/.tar.gz) und Import bestehender Server-Ordner als Instanz.

Sicherheit: Archiv-Einträge werden strikt validiert (keine absoluten Pfade,
kein '..', ':' im Pfad), Gesamtgröße und Dateianzahl sind gedeckelt; tar.gz
wird mit PEP-706-Filter 'data' extrahiert (neutralisiert Traversal/Symlinks).
"""
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

from fastapi import HTTPException

from . import instances
from .config import settings

# Entpackte Gesamtgröße: Welten/ganze Server-Ordner können groß sein, aber
# ohne Deckel wäre ein Zip-Bomben-Angriff möglich (komprimiert nur wenige MiB)
_MAX_EXTRACT_BYTES = 8 * 1024 ** 3
_MAX_MEMBERS = 50_000
_CHUNK = 1024 * 1024

_ARCHIVE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-()\[\]]*\.(zip|tar\.gz)$")
_ZIP_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")

# Welt-Dateien/-Ordner (Vanilla+Nether/End), wenn der Welt-Inhalt direkt im
# Server-Ordner liegt (level.dat auf oberster Ebene) und nach world/ umziehen muss
_WORLD_ITEMS = (
    "level.dat", "level.dat_old", "session.lock", "uid.dat", "icon.png",
    "region", "playerdata", "DIM-1", "DIM1", "datapacks", "stats",
    "advancements",
)


def validate_archive_filename(filename: str) -> str:
    """Server-/Welt-Archive (.zip oder .tar.gz) mit Namensschema prüfen."""
    if not filename or len(filename) > 255:
        raise HTTPException(status_code=400, detail="Ungültiger Archiv-Dateiname")
    if "/" in filename or "\\" in filename or ".." in filename or "\x00" in filename:
        raise HTTPException(status_code=400, detail="Ungültiger Archiv-Dateiname")
    if not _ARCHIVE_RE.match(filename):
        raise HTTPException(status_code=400,
                            detail="Nur .zip- oder .tar.gz-Dateien erlaubt")
    return filename


def staging_dir() -> Path:
    """Zwischenablage im Instanz-Volume (gleiches Dateisystem für Umbenennen)."""
    path = settings.instances_dir / "_staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Extraktion (zip + tar.gz) mit Namens-/Größen-Deckelung
# ---------------------------------------------------------------------------

def _iter_zip(zf: zipfile.ZipFile):
    """Validierte (name, info)-Paare eines Zip-Archivs (Traversal-sicher)."""
    if len(zf.infolist()) > _MAX_MEMBERS:
        raise HTTPException(status_code=400,
                            detail=f"Archiv enthält zu viele Dateien (max. {_MAX_MEMBERS})")
    total = 0
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/") or ":" in name:
            raise HTTPException(status_code=400,
                                detail=f"Unerlaubter Pfad im Archiv: {info.filename!r}")
        if info.is_dir():
            continue
        total += int(info.file_size or 0)
        if total > _MAX_EXTRACT_BYTES:
            raise HTTPException(status_code=413,
                                detail="Archiv-Inhalt zu groß (max. 8 GiB entpackt)")
        yield name, info


def _extract_zip(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    resolved = dest.resolve()
    with zipfile.ZipFile(src) as zf:
        for name, info in _iter_zip(zf):
            target = (dest / name).resolve()
            if not target.is_relative_to(resolved):
                raise HTTPException(status_code=400,
                                    detail=f"Pfadmanipulation im Archiv: {name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src_fh, open(target, "wb") as out:
                shutil.copyfileobj(src_fh, out, _CHUNK)


def _extract_tar(src: Path, dest: Path) -> None:
    import tarfile
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(src, "r:gz") as tf:
        members = tf.getmembers()
        if len(members) > _MAX_MEMBERS:
            raise HTTPException(status_code=400,
                                detail=f"Archiv enthält zu viele Dateien (max. {_MAX_MEMBERS})")
        if sum(m.size for m in members) > _MAX_EXTRACT_BYTES:
            raise HTTPException(status_code=413,
                                detail="Archiv-Inhalt zu groß (max. 8 GiB entpackt)")
        # Filter 'data' (PEP 706): neutralisiert Pfad-Traversal und Symlinks
        tf.extractall(path=dest, filter="data")


def _extract_archive(src: Path, dest: Path, original_name: str) -> None:
    try:
        if original_name.lower().endswith(".tar.gz"):
            _extract_tar(src, dest)
        else:
            _extract_zip(src, dest)
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise HTTPException(status_code=400,
                            detail=f"Archiv nicht entpackbar: {exc}") from exc


def _detect_world(staging: Path):
    """(world_name, world_path) im entpackten Ordner: level.dat am Wurzel-
    verzeichnis (Welt = ganzer Ordner) oder erster Unterordner mit level.dat."""
    if (staging / "level.dat").is_file():
        return None, staging
    try:
        for entry in sorted(staging.iterdir()):
            if entry.is_dir() and not entry.is_symlink() \
                    and (entry / "level.dat").is_file():
                return entry.name, entry
    except OSError:
        pass
    return None, None


def _patch_properties(instance_id: str, updates: dict) -> None:
    """server.properties ergänzen/überschreiben (erzeugt die Datei bei Bedarf)."""
    try:
        props = instances.read_server_properties(instance_id)
    except HTTPException:
        props = []  # Datei existiert noch nicht (frisch importierte Instanz)
    values = {p["key"]: p["value"] for p in props}
    values.update(updates)
    instances.write_server_properties(
        instance_id, [{"key": key, "value": value} for key, value in values.items()],
        managed_ok=True)  # level-name/server-port werden vom Import verwaltet


def _clear_dir(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _move_contents(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dest / child.name
        if target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        shutil.move(str(child), str(target))


# ---------------------------------------------------------------------------
# Welt: Info + Zip-Download + Upload
# ---------------------------------------------------------------------------

def world_info(instance_id: str) -> dict:
    usage = instances.disk_usage(instance_id)
    return {"world_dir": usage["world_dir"], "exists": usage["world_exists"],
            "size_bytes": usage["world_bytes"]}


def create_world_zip(instance_id: str) -> dict:
    """Packt den Welt-Ordner als .zip in die Staging-Ablage (Download)."""
    world = instances.world_dir(instance_id)
    if world is None:
        raise HTTPException(status_code=404,
                            detail="Keine Welt gefunden — Server einmal starten "
                                   "(oder Welt hochladen)")
    safe = _ZIP_NAME_RE.sub("_", world.name) or "world"
    dest = staging_dir() / f"{safe}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.zip"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, _dirs, files in os.walk(world, onerror=None):
            for file in files:
                path = Path(root) / file
                try:
                    zf.write(path, arcname=str(path.relative_to(world)))
                except OSError:
                    continue
    return {"name": dest.name, "path": dest, "world": world.name,
            "size_bytes": dest.stat().st_size}


def restore_world_upload(instance_id: str, src: Path, original_name: str) -> dict:
    """Ersetzt die Welt der (gestoppten) Instanz durch das hochgeladene Archiv.

    Zwei Layouts: Welt-Ordner als Wurzel (level.dat am Anfang) oder Welt als
    einzelner Unterordner — der Unterordner-Name wird übernommen und als
    level-name in server.properties gesetzt.
    """
    from . import runtime  # lazy, vermeidet Import-Zirkel
    instance = instances.get_instance(instance_id)
    if runtime.is_running(instance):
        raise HTTPException(status_code=409,
                            detail="Instanz läuft — Welt-Upload nur bei gestoppter Instanz")
    directory = instances.instance_dir(instance_id)
    staging = Path(tempfile.mkdtemp(prefix="world_", dir=staging_dir()))
    try:
        _extract_archive(src, staging, original_name)
        world_name, world_path = _detect_world(staging)
        if world_path is None:
            raise HTTPException(status_code=400,
                                detail="Kein Welt-Ordner gefunden (level.dat fehlt) — "
                                       "die .zip muss die Welt oder deren Inhalt enthalten")
        if world_name is None:
            # Welt-Inhalt direkt im Archiv-Wurzelverzeichnis: in den
            # bestehenden Welt-Ordner (level-name bzw. 'world') entpacken
            target = instances.find_world_dir(directory) or (directory / "world")
        else:
            # Alten Welt-Ordner (abweichender Name) entfernen, damit keine
            # verwaiste Doppel-Welt im Instanz-Ordner bleibt
            previous = instances.find_world_dir(directory)
            if previous is not None and previous.name != world_name:
                shutil.rmtree(previous, ignore_errors=True)
            target = directory / world_name
        _clear_dir(target)
        _move_contents(world_path, target)
        # level-name immer auf den Ziel-Ordner setzen, damit die hochgeladene
        # Welt garantiert verwendet wird (auch wenn Properties abweichen)
        _patch_properties(instance_id, {"level-name": target.name})
        return {"world_dir": target.name, "restored": original_name}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# Server-Import: bestehenden Server-Ordner als neue Instanz einbinden
# ---------------------------------------------------------------------------

def import_server(src: Path, original_name: str, *, name: str, loader: str,
                  game_version: str, loader_version: str = None,
                  port: int = None, memory: str = None,
                  accept_eula: bool = False) -> dict:
    """Erstellt eine neue Instanz aus einem hochgeladenen Server-Archiv
    (.zip/.tar.gz mit Welt/Mods/Configs).

    Die Archiv-Dateien landen im Instanz-Ordner; server.properties wird auf
    die Container-Erfordernisse angepasst (Port 25565 im Container, RCON),
    eula.txt erzwungen akzeptiert und eine gefundene Welt verdrahtet.
    Bei Fehlern wird die angelegte Instanz vollständig entfernt.
    """
    instance = instances.create_instance(
        name, loader, game_version, loader_version=loader_version, port=port,
        memory=memory, accept_eula=accept_eula)
    directory = instances.instance_dir(instance["id"])
    staging = Path(tempfile.mkdtemp(prefix="import_", dir=staging_dir()))
    try:
        _extract_archive(src, staging, original_name)
        _move_contents(staging, directory)
        # Archiv könnte versehentlich ein instance.json mitbringen — unsere
        # frische Metadaten erzwingen (unser ID/Port/Status gewinnt)
        instances.update_instance(instance)
        world_name, world_path = _detect_world(directory)
        if world_name is None and world_path is not None:
            # Welt-Inhalt liegt direkt im Server-Ordner → nach world/ umziehen
            world_target = directory / "world"
            world_target.mkdir(exist_ok=True)
            for item in _WORLD_ITEMS:
                child = directory / item
                if child.exists():
                    target = world_target / item
                    if target.exists():
                        if target.is_dir() and not target.is_symlink():
                            shutil.rmtree(target, ignore_errors=True)
                        else:
                            target.unlink(missing_ok=True)
                    shutil.move(str(child), str(target))
            world_name = "world"
        updates = {"server-port": "25565", "enable-rcon": "true",
                   "rcon.port": "25575"}
        if world_name:
            updates["level-name"] = world_name
        _patch_properties(instance["id"], updates)
        # EULA der Instanz erzwingen (Archiv könnte eula=false mitbringen)
        (directory / "eula.txt").write_text("eula=true\n", encoding="utf-8")
        return instance
    except HTTPException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    except (OSError, ValueError) as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(status_code=500,
                            detail=f"Import fehlgeschlagen: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
