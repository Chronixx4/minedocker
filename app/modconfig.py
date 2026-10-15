"""Mod-Einstellungen: Konfigurationsdateien der Mods/Plugins lesen und ändern.

- Gelistet werden Textdateien unter config/ (Mods) und plugins/<Name>/
  (Plugins) — bewusst nur diese Ordner.
- .properties und .toml werden zeilenweise geparst: jede Einstellung wird mit
  Typ (bool/int/float/string/raw) und dem Kommentar darüber angeboten.
  Beim Speichern ändert sich nur die Wertspalte der betroffenen Zeilen;
  Kommentare, Reihenfolge und Formatierung bleiben erhalten.
- Alle anderen Formate (json, yml, cfg …) gibt es nur als Rohtext über den
  Datei-Browser.
- Schreiben prüft einen Fingerabdruck (sha1): hat sich die Datei inzwischen
  geändert (Mod hat sie neu geschrieben), antwortet der Server mit 409.
"""
import hashlib
import json
import re

from fastapi import HTTPException

from . import filebrowser

ROOTS = ("config", "plugins")
STRUCTURED = (".properties", ".toml")
LISTED = (*STRUCTURED, ".json", ".json5", ".yml", ".yaml", ".cfg", ".conf", ".txt")
MAX_FILE = 512 * 1024
MAX_FILES = 400

_TOML_SECTION = re.compile(r"^\s*\[\[?([^\]]+)\]\]?\s*(?:#.*)?$")
_TOML_KV = re.compile(r'^(\s*)([A-Za-z0-9_.\-"\']+)(\s*=\s*)(.*)$')
_PROP_KV = re.compile(r"^(\s*)([^\s#!=:][^=:]*?)(\s*=\s*)(.*)$")
_QUOTED = re.compile(r'^"((?:[^"\\]|\\.)*)"(.*)$')
_INT = re.compile(r"^[+-]?\d[\d_]*$")
_FLOAT = re.compile(r"^[+-]?\d[\d_]*\.\d+(?:[eE][+-]?\d+)?$")


def sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _group_of(rel: str) -> str:
    parts = rel.split("/")
    if parts[0] == "plugins":
        return parts[1] if len(parts) > 2 else "plugins"
    return parts[1] if len(parts) > 2 else "(allgemein)"


def check_path(rel: str) -> str:
    if rel.split("/")[0] not in ROOTS or not rel.lower().endswith(LISTED):
        raise HTTPException(
            status_code=400,
            detail="Nur Dateien unter config/ und plugins/ sind als Mod-Einstellung erlaubt")
    return rel


def list_configs(instance_id: str) -> list:
    base = filebrowser._base(instance_id)
    out: list = []
    for root in ROOTS:
        top = base / root
        if not top.is_dir():
            continue
        depth_limit = 3 if root == "config" else 2
        try:
            candidates = sorted(top.rglob("*"))
        except OSError:
            continue
        for path in candidates:
            try:
                rel_parts = path.relative_to(base).parts
                if (len(rel_parts) - 1 > depth_limit or not path.is_file()
                        or path.is_symlink()
                        or any(p.startswith(".") for p in rel_parts)
                        or path.suffix.lower() not in LISTED):
                    continue
                size = path.stat().st_size
            except (OSError, ValueError):
                continue
            if size > MAX_FILE:
                continue
            rel = "/".join(rel_parts)
            out.append({"path": rel, "name": path.name, "group": _group_of(rel),
                        "size": size,
                        "structured": path.suffix.lower() in STRUCTURED})
            if len(out) >= MAX_FILES:
                return out
    out.sort(key=lambda e: (e["group"].lower(), e["path"].lower()))
    return out


def _toml_value(raw: str) -> tuple:
    """(Anzeige-Wert, Typ, Rest hinter dem Wert) für den rechten Teil einer TOML-Zeile."""
    raw = raw.strip()
    match = _QUOTED.match(raw)
    if match:
        try:
            value = json.loads(f'"{match.group(1)}"')
        except ValueError:
            value = match.group(1)
        return value, "string", match.group(2)
    if raw.startswith(("[", "{", "'")):
        return raw, "raw", ""
    body, _, comment = raw.partition("#")
    body = body.strip()
    rest = f" #{comment}" if comment else ""
    if body in ("true", "false"):
        return body, "bool", rest
    if _INT.match(body):
        return body, "int", rest
    if _FLOAT.match(body):
        return body, "float", rest
    return body, "raw", rest


def parse(text: str, fmt: str) -> list:
    entries = []
    section = ""
    comment: list = []
    for number, line in enumerate(text.split("\n"), start=1):
        stripped = line.strip()
        if not stripped:
            comment = []
            continue
        if stripped.startswith(("#", "!")):
            comment.append(stripped.lstrip("#! ").strip())
            continue
        if fmt == ".toml":
            sec = _TOML_SECTION.match(line)
            if sec:
                section = sec.group(1).strip()
                comment = []
                continue
            kv = _TOML_KV.match(line)
            if not kv:
                comment = []
                continue
            value, kind, _rest = _toml_value(kv.group(4))
        else:
            kv = _PROP_KV.match(line)
            if not kv:
                comment = []
                continue
            raw = kv.group(4).rstrip("\r")
            low = raw.strip().lower()
            if low in ("true", "false"):
                value, kind = low, "bool"
            elif _INT.match(raw.strip()):
                value, kind = raw.strip(), "int"
            elif _FLOAT.match(raw.strip()):
                value, kind = raw.strip(), "float"
            else:
                value, kind = raw, "string"
        entries.append({"line": number, "section": section,
                        "key": kv.group(2).strip("\"'"), "value": value,
                        "type": kind, "comment": " ".join(comment)[:300]})
        comment = []
    return entries


def _format_value(value, kind: str, fmt: str) -> str:
    text = "" if value is None else str(value)
    if "\n" in text or "\r" in text:
        raise HTTPException(status_code=400, detail="Werte dürfen keine Zeilenumbrüche enthalten")
    if kind == "bool":
        low = text.strip().lower()
        if low not in ("true", "false"):
            raise HTTPException(status_code=400, detail="Ein Wahrheitswert muss true oder false sein")
        return low
    if kind == "int":
        if not _INT.match(text.strip()):
            raise HTTPException(status_code=400, detail=f"'{text}' ist keine ganze Zahl")
        return text.strip()
    if kind == "float":
        if not (_FLOAT.match(text.strip()) or _INT.match(text.strip())):
            raise HTTPException(status_code=400, detail=f"'{text}' ist keine Zahl")
        return text.strip()
    if fmt == ".toml" and kind == "string":
        return json.dumps(text, ensure_ascii=False)
    return text


def apply_changes(text: str, fmt: str, changes: list) -> str:
    """Zeilen laut changes [{line, key, value}] ersetzen (Schlüssel und Typ
    werden gegen die aktuelle Datei geprüft)."""
    lines = text.split("\n")
    current = {e["line"]: e for e in parse(text, fmt)}
    for change in changes:
        number = int(change.get("line") or 0)
        entry = current.get(number)
        if entry is None or entry["key"] != change.get("key"):
            raise HTTPException(status_code=409,
                                detail="Die Datei hat sich geändert — bitte neu laden")
        new = _format_value(change.get("value"), entry["type"], fmt)
        line = lines[number - 1]
        if fmt == ".toml":
            kv = _TOML_KV.match(line)
            _v, _k, rest = _toml_value(kv.group(4))  # type: ignore[union-attr]
            lines[number - 1] = f"{kv.group(1)}{kv.group(2)}{kv.group(3)}{new}{rest}"  # type: ignore[union-attr]
        else:
            kv = _PROP_KV.match(line.rstrip("\r"))
            eol = "\r" if line.endswith("\r") else ""
            lines[number - 1] = f"{kv.group(1)}{kv.group(2)}{kv.group(3)}{new}{eol}"  # type: ignore[union-attr]
    return "\n".join(lines)


def read_file(instance_id: str, raw_path: str) -> dict:
    _path, rel = filebrowser.resolve_path(instance_id, raw_path)
    check_path(rel)
    data = filebrowser.read_text(instance_id, rel)
    fmt = "." + rel.rsplit(".", 1)[-1].lower()
    structured = fmt in STRUCTURED
    return {"path": rel, "format": fmt.lstrip("."), "structured": structured,
            "sha": sha(data["content"]),
            "entries": parse(data["content"], fmt) if structured else [],
            "content": None if structured else data["content"]}


def save_changes(instance_id: str, raw_path: str, expected_sha: str, changes: list) -> dict:
    _path, rel = filebrowser.resolve_path(instance_id, raw_path)
    check_path(rel)
    fmt = "." + rel.rsplit(".", 1)[-1].lower()
    if fmt not in STRUCTURED:
        raise HTTPException(status_code=400,
                            detail="Dieses Format gibt es nur als Rohtext (Datei-Browser)")
    if not changes:
        raise HTTPException(status_code=400, detail="Keine Änderungen")
    data = filebrowser.read_text(instance_id, rel)
    if sha(data["content"]) != expected_sha:
        raise HTTPException(status_code=409,
                            detail="Die Datei hat sich geändert — bitte neu laden")
    new_text = apply_changes(data["content"], fmt, changes)
    filebrowser.write_text(instance_id, rel, new_text)
    return read_file(instance_id, rel)
