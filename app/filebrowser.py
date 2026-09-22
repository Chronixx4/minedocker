"""Datei-Browser je Instanz: CRUD auf dem Instanz-Ordner mit Pfadschutz.

Alle Pfade werden relativ zum Instanz-Ordner ({INSTANCES_DIR}/{id}/)
aufgelöst — mit resolve()-Doppelprüfung gegen Traversal (Muster
safe_mods_path) und Symlink-Escapes (resolve folgt Links; verlässt der
aufgelöste Pfad den Instanz-Ordner, wird blockiert).

Verwaltete Dateien bleiben außen vor:
- server.properties und instance.json: komplett ausgeschlossen (lesbar und
  schreibbar nur über den validierten Einstellungs-Editor bzw. die
  Instanz-Metadaten) — 400 mit Verweis.
- alles unter packs/: nur lesbar (interner Installer-Speicher; Löschen
  würde die Installations-Quelle zerstören).

Grenzen: Textdateien ≤ 1 MiB und UTF-8-decodierbar (NUL-Probe erkennt
Binärdateien), Upload-Deckel über FILEBROWSER_MAX_UPLOAD_MB (Default 300),
keine rekursiven Löschungen (Ordner nur leer), Overwrite nur explizit.
"""
import os
from pathlib import Path

from fastapi import HTTPException

# Text-Edit-Limit (Bytes) für GET/PUT content
TEXT_LIMIT = 1 * 1024 * 1024
_MAX_PATH_LEN = 512

# Diese Ziele dürfen im Browser weder gelesen noch geschrieben werden
# (dedizierte Validierungs-Editoren/verwaltete Metadaten)
_MANAGED_FILES = ("server.properties", "instance.json")
# Nur lesbar (interner Installer-Speicher der Instanz)
_READONLY_PREFIX = "packs/"

_HINT_PROPS = ("server.properties wird über den Einstellungs-Editor "
               "verwaltet (Konsole → Einstellungen)")
_HINT_META = "instance.json wird vom Dashboard verwaltet"
_HINT_PACKS = ("Der packs-Ordner ist nur lesbar — Modpack-Archive sind "
               "Installations-Quellen des Dashboards")


def _base(instance_id: str) -> Path:
    """Instanz-Ordner (prüft die ID, nicht den Pfad)."""
    from .instances import instance_dir  # lazy, vermeidet Import-Zirkel
    return instance_dir(instance_id).resolve()


def _hint_for(rel: str) -> str:
    if rel == "server.properties":
        return _HINT_PROPS
    if rel == "instance.json":
        return _HINT_META
    return _HINT_PACKS


def normalize_rel_path(raw: str) -> str:
    """Relativen Browser-Pfad säubern: Backslashes → Slashes, leere Segmente
    und '.'-Segmente entfernen; '..' und Absolutes werden abgelehnt.
    Rückgabe: kanonischer relativer Pfad mit '/'-Trennern ("" = Wurzel)."""
    raw = (raw or "").strip()
    if len(raw) > _MAX_PATH_LEN:
        raise HTTPException(status_code=400, detail="Pfad zu lang")
    if "\x00" in raw:
        raise HTTPException(status_code=400, detail="Ungültiger Pfad")
    raw = raw.replace("\\", "/")
    if raw.startswith("/"):
        raise HTTPException(status_code=400,
                            detail="Absoluter Pfad nicht erlaubt — Pfad ist "
                                   "relativ zum Instanz-Ordner")
    if ":" in raw.split("/")[0] and len(raw.split("/")[0]) <= 2:
        raise HTTPException(status_code=400, detail="Laufwerksbuchstabe nicht erlaubt")
    parts = []
    for segment in raw.split("/"):
        segment = segment.strip()
        if not segment or segment == ".":
            continue
        if segment == "..":
            raise HTTPException(status_code=400, detail="Pfadmanipulation erkannt")
        parts.append(segment)
    return "/".join(parts)


def resolve_path(instance_id: str, raw: str) -> tuple:
    """(absoluter Pfad, kanonischer relativer Pfad). Doppelprüfung: Nach dem
    resolve() muss das Ziel innerhalb des Instanz-Ordners liegen (blockt
    '..'-Reste und Symlink-Escapes)."""
    base = _base(instance_id)
    rel = normalize_rel_path(raw)
    path = (base / rel).resolve() if rel else base
    if path != base and not path.is_relative_to(base):
        raise HTTPException(status_code=400, detail="Pfadmanipulation erkannt")
    return path, rel


def check_writable(rel: str) -> None:
    """Schreib-Operationen auf verwaltete Ziele blockieren (400 mit Hinweis)."""
    if rel in _MANAGED_FILES or rel.startswith(_READONLY_PREFIX) \
            or rel == "packs":
        raise HTTPException(status_code=400,
                             detail=f"'{rel or '.'}' ist geschützt: {_hint_for(rel)}")


def check_readable(rel: str) -> None:
    """Lesen: server.properties/instance.json ausgeschlossen (dedizierte
    Editoren), packs/ erlaubt."""
    if rel in _MANAGED_FILES:
        raise HTTPException(status_code=400,
                             detail=f"'{rel}' ist geschützt: {_hint_for(rel)}")


# ---------------------------------------------------------------------------
# Operationen
# ---------------------------------------------------------------------------

def list_dir(instance_id: str, raw: str) -> dict:
    """Ordnerinhalt auflisten (Ordner zuerst, Name aufsteigend; verwaltete
    Einträge mit managed-Flag, damit die UI sie ausgrauen kann)."""
    path, rel = resolve_path(instance_id, raw)
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="Ordner nicht gefunden")
    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Ordner nicht lesbar: {exc}")
    out = []
    for entry in entries:
        try:
            info = entry.stat()
            size = info.st_size if entry.is_file() else None
            mtime = int(info.st_mtime)
        except OSError:
            continue  # verschwundene/unteilbare Einträge überspringen
        entry_rel = f"{rel}/{entry.name}" if rel else entry.name
        out.append({
            "name": entry.name,
            "type": "dir" if entry.is_dir() else "file",
            "size": size,
            "mtime": mtime,
            "managed": entry_rel in _MANAGED_FILES
                       or entry_rel.startswith(_READONLY_PREFIX),
        })
    return {"path": rel, "entries": out}


def _read_text_checked(path: Path) -> tuple:
    """Datei als Text lesen (Limits + Binär-Erkennung). Rückgabe (text, size)."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Datei nicht gefunden") from exc
    if size > TEXT_LIMIT:
        raise HTTPException(status_code=400,
                            detail=f"Datei zu groß für den Text-Editor "
                                   f"(max. {TEXT_LIMIT // (1024 * 1024)} MiB) — "
                                   "herunterladen statt bearbeiten")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Datei nicht lesbar: {exc}")
    if b"\x00" in data:
        raise HTTPException(status_code=400,
                            detail="Binärdatei — nicht als Text bearbeitbar")
    try:
        return data.decode("utf-8"), size
    except UnicodeDecodeError:
        raise HTTPException(status_code=400,
                            detail="Binärdatei oder kein UTF-8 — nicht als "
                                   "Text bearbeitbar")


def read_text(instance_id: str, raw: str) -> dict:
    path, rel = resolve_path(instance_id, raw)
    check_readable(rel)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Datei nicht gefunden")
    text, size = _read_text_checked(path)
    return {"path": rel, "size": size, "content": text}


def write_text(instance_id: str, raw: str, content: str) -> dict:
    """Textdatei atomar speichern (tmp + os.replace); Overwrite immer
    erlaubt hier — versehentliches Überschreiben verhindert die UI, indem
    sie vorher liest."""
    path, rel = resolve_path(instance_id, raw)
    check_writable(rel)
    if not rel:
        raise HTTPException(status_code=400, detail="Zieldatei fehlt")
    data = (content or "").encode("utf-8")
    if len(data) > TEXT_LIMIT:
        raise HTTPException(status_code=400,
                            detail=f"Inhalt zu groß (max. {TEXT_LIMIT // (1024 * 1024)} MiB)")
    if not path.parent.is_dir():
        raise HTTPException(status_code=400, detail="Übergeordneter Ordner fehlt")
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=500,
                            detail=f"Datei nicht schreibbar: {exc}") from exc
    return {"path": rel, "size": len(data)}


def download_path(instance_id: str, raw: str) -> tuple:
    """Pfad für einen Download prüfen (Datei, keine verwalteten Dateien)."""
    path, rel = resolve_path(instance_id, raw)
    check_readable(rel)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Datei nicht gefunden")
    return path, rel


def upload_dest(instance_id: str, raw: str, overwrite: bool) -> Path:
    """Ziel für einen Upload bestimmen (muss eine Datei sein; 409 bei
    Kollision ohne overwrite)."""
    path, rel = resolve_path(instance_id, raw)
    check_writable(rel)
    if not rel or rel.endswith("/"):
        raise HTTPException(status_code=400, detail="Zieldatei fehlt")
    if path.is_dir():
        raise HTTPException(status_code=400,
                            detail="Ziel ist ein Ordner — vollen Dateipfad angeben")
    if path.exists() and not overwrite:
        raise HTTPException(status_code=409,
                            detail=f"'{rel}' existiert bereits (overwrite=true "
                                   "zum Überschreiben)")
    if not path.parent.is_dir():
        raise HTTPException(status_code=400, detail="Übergeordneter Ordner fehlt")
    return path


def mkdir(instance_id: str, raw: str) -> dict:
    """Einen Ordner anlegen (keine rekursiven Eltern)."""
    path, rel = resolve_path(instance_id, raw)
    check_writable(rel)
    if not rel:
        raise HTTPException(status_code=400, detail="Ordnername fehlt")
    if path.exists():
        raise HTTPException(status_code=409, detail=f"'{rel}' existiert bereits")
    try:
        path.mkdir()  # ohne parents: genau eine Ebene
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400,
                            detail="Übergeordneter Ordner fehlt") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Ordner nicht anlegbar: {exc}")
    return {"created": rel}


def rename(instance_id: str, raw_from: str, raw_to: str) -> dict:
    """Umbenennen/verschieben innerhalb der Instanz; Ziel darf nicht
    existieren und nicht auf ein geschütztes Ziel zeigen."""
    src, rel_from = resolve_path(instance_id, raw_from)
    dst, rel_to = resolve_path(instance_id, raw_to)
    check_writable(rel_from)
    check_writable(rel_to)
    if not rel_from or not rel_to:
        raise HTTPException(status_code=400, detail="Quelle und Ziel angeben")
    if not src.exists() or src == _base(instance_id):
        raise HTTPException(status_code=404, detail="Quelle nicht gefunden")
    if dst.exists():
        raise HTTPException(status_code=409, detail=f"'{rel_to}' existiert bereits")
    if dst.parent != src.parent and not dst.parent.is_dir():
        raise HTTPException(status_code=400, detail="Ziel-Ordner fehlt")
    try:
        os.replace(src, dst)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Umbenennen fehlgeschlagen: {exc}")
    return {"renamed": rel_from, "to": rel_to}


def delete(instance_id: str, raw: str) -> dict:
    """Löschen: Dateien jederzeit; Ordner nur wenn leer (keine rekursiven
    Löschungen); Wurzel und geschützte Ziele blockiert."""
    path, rel = resolve_path(instance_id, raw)
    base = _base(instance_id)
    check_writable(rel)
    if not rel or path == base:
        raise HTTPException(status_code=400, detail="Instanz-Ordner kann nicht "
                                                   "gelöscht werden")
    if not path.exists() and not path.is_symlink():
        raise HTTPException(status_code=404, detail="Nicht gefunden")
    try:
        if path.is_dir() and not path.is_symlink():
            if any(path.iterdir()):
                raise HTTPException(status_code=409,
                                    detail="Ordner ist nicht leer — rekursive "
                                           "Löschungen sind deaktiviert")
            path.rmdir()
        else:
            path.unlink()
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Löschen fehlgeschlagen: {exc}")
    return {"deleted": rel}


__all__ = [
    "check_readable",
    "check_writable",
    "delete",
    "download_path",
    "list_dir",
    "mkdir",
    "normalize_rel_path",
    "read_text",
    "rename",
    "resolve_path",
    "upload_dest",
    "write_text",
]
