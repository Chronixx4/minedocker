"""Mod-Metadaten aus .jar-Dateien und Problem-Erkennung für die Mod-Liste.

Liest fabric.mod.json, quilt.mod.json und (neoforge.)mods.toml direkt aus
der .jar (offline, ohne Modrinth/CurseForge) und prüft die Mods einer
Instanz auf typische Startabbrüche: fehlende Pflicht-Abhängigkeit, falscher
Loader, reine Client-Mod, doppelt vorhandene Mod-ID.

Mitgelieferte Bibliotheken (Fabric "jars", Forge "jarjar") werden eine
Ebene tief mitgelesen, damit z. B. Module der Fabric API als vorhanden
gelten.
"""
from __future__ import annotations

import io
import json
import re
import threading
import tomllib
import zipfile
from pathlib import Path

# Abhängigkeiten, die nie als Mod-Datei im mods-Ordner liegen
_BUILTIN_IDS = frozenset({
    "minecraft", "java", "fabricloader", "fabric-loader", "quilt_loader",
    "forge", "neoforge", "fml", "javafml", "lowcodefml", "mixinextras",
})
# Alte Fabric-API-ID, die von der heutigen fabric-api abgedeckt wird
_ALIASES = {"fabric": "fabric-api"}

_MAX_META_BYTES = 512 * 1024
_MAX_NESTED_BYTES = 8 * 1024 * 1024

_CACHE: dict[tuple, dict | None] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 4096


def _authors(value) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        value = [a.strip() for a in value.split(",")]
    if isinstance(value, dict):
        value = list(value.keys())
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, dict):
            item = item.get("name")
        if isinstance(item, str) and item.strip():
            out.append(item.strip()[:80])
    return out[:8]


def _text(value, limit: int = 300) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] or None


def _manifest_version(zf: zipfile.ZipFile) -> str | None:
    try:
        raw = zf.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except (KeyError, OSError):
        return None
    match = re.search(r"^Implementation-Version:\s*(\S+)", raw, re.MULTILINE)
    return match.group(1) if match else None


def _read_small(zf: zipfile.ZipFile, name: str, limit: int) -> bytes | None:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > limit:
        return None
    try:
        return zf.read(info)
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return None


def _parse_fabric(obj: dict) -> dict:
    deps = []
    for dep_id, rng in (obj.get("depends") or {}).items():
        if isinstance(rng, list):
            rng = " || ".join(str(r) for r in rng)
        deps.append({"id": str(dep_id), "version": str(rng) if rng else "*"})
    env = obj.get("environment")
    return {
        "loader": "fabric",
        "mod_id": obj.get("id"),
        "name": obj.get("name"),
        "version": obj.get("version"),
        "description": obj.get("description"),
        "authors": _authors(obj.get("authors")),
        "environment": env if env in ("client", "server") else "both",
        "depends": deps,
        "provides": [str(p) for p in (obj.get("provides") or []) if isinstance(p, str)],
        "nested": [j.get("file") for j in (obj.get("jars") or [])
                   if isinstance(j, dict) and isinstance(j.get("file"), str)],
    }


def _parse_quilt(obj: dict) -> dict:
    ql = obj.get("quilt_loader") or {}
    meta = ql.get("metadata") or {}
    deps = []
    for dep in ql.get("depends") or []:
        if isinstance(dep, str):
            deps.append({"id": dep, "version": "*"})
        elif isinstance(dep, dict) and isinstance(dep.get("id"), str) \
                and not dep.get("optional"):
            versions = dep.get("versions")
            if isinstance(versions, list):
                versions = " || ".join(str(v) for v in versions)
            deps.append({"id": dep["id"], "version": str(versions or "*")})
    provides = []
    for item in ql.get("provides") or []:
        if isinstance(item, str):
            provides.append(item)
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            provides.append(item["id"])
    env = ((obj.get("minecraft") or {}).get("environment") or "*")
    return {
        "loader": "quilt",
        "mod_id": ql.get("id"),
        "name": meta.get("name"),
        "version": ql.get("version"),
        "description": meta.get("description"),
        "authors": _authors(meta.get("contributors")),
        "environment": {"client": "client", "dedicated_server": "server"}.get(env, "both"),
        "depends": deps,
        "provides": provides,
        "nested": [j for j in (ql.get("jars") or []) if isinstance(j, str)],
    }


def _parse_forge(obj: dict, loader: str, jar_version: str | None) -> dict:
    mods = [m for m in (obj.get("mods") or []) if isinstance(m, dict)]
    first = mods[0] if mods else {}
    own_ids = {m.get("modId") for m in mods if isinstance(m.get("modId"), str)}
    deps = []
    raw_deps = obj.get("dependencies") or {}
    if isinstance(raw_deps, dict):
        for owner, entries in raw_deps.items():
            if owner not in own_ids or not isinstance(entries, list):
                continue
            for dep in entries:
                if not isinstance(dep, dict) or not isinstance(dep.get("modId"), str):
                    continue
                # Forge: mandatory=true · NeoForge: type="required"
                required = dep.get("mandatory") is True \
                    or str(dep.get("type") or "").lower() == "required"
                side = str(dep.get("side") or "BOTH").upper()
                if required and side in ("BOTH", "SERVER"):
                    deps.append({"id": dep["modId"],
                                 "version": str(dep.get("versionRange") or "*")})
    version = first.get("version")
    if isinstance(version, str) and "${" in version:
        version = jar_version
    return {
        "loader": loader,
        "mod_id": first.get("modId"),
        "name": first.get("displayName"),
        "version": version,
        "description": first.get("description"),
        "authors": _authors(first.get("authors") or obj.get("authors")),
        # Forge-Mods deklarieren keine Umgebung zuverlässig
        "environment": None,
        "depends": deps,
        "provides": sorted(i for i in own_ids if i != first.get("modId")),
        "nested": [],
    }


def _parse_zip(zf: zipfile.ZipFile) -> dict | None:
    """Metadaten der ersten erkannten Loader-Datei (plus weitere Loader bei
    Multi-Loader-Jars)."""
    names = set(zf.namelist())
    found: list[dict] = []
    for meta_name, kind in (("fabric.mod.json", "fabric"),
                            ("quilt.mod.json", "quilt"),
                            ("META-INF/quilt.mod.json", "quilt"),
                            ("META-INF/neoforge.mods.toml", "neoforge"),
                            ("META-INF/mods.toml", "forge")):
        if meta_name not in names:
            continue
        raw = _read_small(zf, meta_name, _MAX_META_BYTES)
        if raw is None:
            continue
        text = raw.decode("utf-8", "replace")
        try:
            if kind == "fabric":
                # Fabric erlaubt Steuerzeichen in Strings (strict=False)
                found.append(_parse_fabric(json.loads(text, strict=False)))
            elif kind == "quilt":
                found.append(_parse_quilt(json.loads(text, strict=False)))
            else:
                found.append(_parse_forge(tomllib.loads(text), kind,
                                          _manifest_version(zf)))
        except (ValueError, tomllib.TOMLDecodeError, AttributeError, TypeError):
            continue
    if not found:
        return None
    meta = found[0]
    loaders = []
    for item in found:
        if item["loader"] not in loaders:
            loaders.append(item["loader"])
    meta["loaders"] = loaders
    # Forge/NeoForge: mitgelieferte Jars (Jar-in-Jar)
    nested = list(meta.pop("nested", []))
    for item in found[1:]:
        item.pop("nested", None)
    nested.extend(n for n in names
                  if n.startswith("META-INF/jarjar/") and n.endswith(".jar"))
    meta["_nested"] = nested
    return meta


def _clean(meta: dict) -> dict:
    out = {
        "mod_id": meta.get("mod_id") if isinstance(meta.get("mod_id"), str) else None,
        "name": _text(meta.get("name"), 80),
        "version": _text(meta.get("version"), 60),
        "description": _text(meta.get("description")),
        "authors": meta.get("authors") or [],
        "loaders": meta.get("loaders") or [],
        "environment": meta.get("environment"),
        "depends": meta.get("depends") or [],
        "provides": meta.get("provides") or [],
    }
    return out


def _read(path: Path) -> dict | None:
    try:
        with zipfile.ZipFile(path) as zf:
            meta = _parse_zip(zf)
            if meta is None:
                return None
            provides = set(meta.get("provides") or [])
            for name in meta.pop("_nested", []):
                raw = _read_small(zf, name, _MAX_NESTED_BYTES)
                if raw is None:
                    continue
                try:
                    with zipfile.ZipFile(io.BytesIO(raw)) as inner:
                        sub = _parse_zip(inner)
                except (zipfile.BadZipFile, OSError, ValueError):
                    continue
                if sub:
                    if isinstance(sub.get("mod_id"), str):
                        provides.add(sub["mod_id"])
                    provides.update(sub.get("provides") or [])
            meta["provides"] = sorted(provides)
            return _clean(meta)
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
        return None


def read_meta(path: Path) -> dict | None:
    """Metadaten einer Mod-Datei (gecacht über Pfad, Größe und mtime) oder
    None, wenn die Datei keine bekannte Loader-Beschreibung enthält."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, st.st_mtime_ns)
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
    meta = _read(path)
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = meta
    return meta


# ---------------------------------------------------------------------------
# Problem-Erkennung
# ---------------------------------------------------------------------------

def _loader_ok(mod_loaders: list, loader: str, game_version: str) -> bool:
    if not mod_loaders:
        return True
    accepted = {"fabric": {"fabric"}, "quilt": {"fabric", "quilt"},
                "forge": {"forge"}, "neoforge": {"neoforge"}}.get(loader)
    if accepted is None:
        return True  # Paper/Bukkit & Co.: keine Mod-Loader-Prüfung
    if loader == "neoforge" and game_version == "1.20.1":
        accepted = {"neoforge", "forge"}  # NeoForge 1.20.1 lädt Forge-Mods
    return bool(accepted.intersection(mod_loaders))


_LOADER_LABEL = {"fabric": "Fabric", "quilt": "Quilt", "forge": "Forge",
                 "neoforge": "NeoForge"}


def analyze(mods: list, loader: str, game_version: str) -> list:
    """Ergänzt jede Mod (Ergebnis von list_mods_in mit 'meta') um
    'problems' und liefert die Gesamtliste der Probleme zurück.

    Problem-Typen: missing_dependency, disabled_dependency, wrong_loader,
    client_only, duplicate."""
    loader = (loader or "").lower()
    if loader not in _LOADER_LABEL:
        for mod in mods:
            mod["problems"] = []
        return []
    enabled_ids: dict[str, list[str]] = {}
    disabled_ids: dict[str, str] = {}
    own_ids: dict[str, list[str]] = {}  # nur die Haupt-ID, für Duplikate
    for mod in mods:
        meta = mod.get("meta") or {}
        if mod["enabled"] and meta.get("mod_id"):
            own_ids.setdefault(meta["mod_id"], []).append(mod["filename"])
        ids = set(meta.get("provides") or [])
        if meta.get("mod_id"):
            ids.add(meta["mod_id"])
        for mod_id in ids:
            if mod["enabled"]:
                enabled_ids.setdefault(mod_id, []).append(mod["filename"])
            else:
                disabled_ids.setdefault(mod_id, mod["filename"])

    problems: list = []
    for mod in mods:
        mod["problems"] = []
        meta = mod.get("meta")
        if not mod["enabled"] or not meta:
            continue
        name = meta.get("name") or mod["filename"]

        def add(kind: str, message: str, mod: dict = mod, **extra) -> None:
            entry = {"type": kind, "filename": mod["filename"],
                     "message": message, **extra}
            mod["problems"].append(entry)
            problems.append(entry)

        if not _loader_ok(meta.get("loaders") or [], loader, game_version):
            mod_loaders = ", ".join(_LOADER_LABEL.get(x, x) for x in meta["loaders"])
            add("wrong_loader",
                f"{name} ist für {mod_loaders}, der Server nutzt {_LOADER_LABEL[loader]}")
            continue  # Abhängigkeiten eines falschen Loaders sind bedeutungslos
        if meta.get("environment") == "client":
            add("client_only", f"{name} ist eine reine Client-Mod und kann den "
                               f"Serverstart verhindern")
        for dep in meta.get("depends") or []:
            dep_id = dep["id"]
            if dep_id in _BUILTIN_IDS:
                continue
            if dep_id in enabled_ids or _ALIASES.get(dep_id) in enabled_ids:
                continue
            version = dep.get("version") or "*"
            suffix = f" ({version})" if version not in ("*", "") else ""
            if dep_id in disabled_ids:
                add("disabled_dependency",
                    f"{name} braucht {dep_id}{suffix}, das ist aber deaktiviert",
                    dependency=dep_id, provided_by=disabled_ids[dep_id])
            else:
                add("missing_dependency",
                    f"{name} braucht {dep_id}{suffix}, nicht installiert",
                    dependency=dep_id)
        mod_id = meta.get("mod_id")
        files = own_ids.get(mod_id or "", [])
        if mod_id and len(files) > 1 and files[0] != mod["filename"]:
            add("duplicate", f"{name} ist doppelt vorhanden ({', '.join(files)})",
                mod_id=mod_id)
    return problems
