"""Item-Icons und -Namen für die Inventar-Ansicht.

Quellen:
- Mod-Items: Texturen, Modelle und Sprachdateien direkt aus den Mod-JARs im
  mods-Ordner der Instanz (assets/<modid>/…).
- Vanilla-Items: die Server-JAR enthält keine Texturen. Deshalb wird einmal
  pro MC-Version die Client-JAR von Mojang geladen und nur der benötigte Teil
  (Item-/Block-Texturen, Modelle, Sprachdateien) als kleines ZIP unter
  {INSTANCES_DIR}/.itemcache/ abgelegt. Abschaltbar mit ITEM_ICONS_VANILLA=false.

Icon-Auflösung: items/<name>.json (ab 1.21.4) → models/item/<name>.json →
Eltern-Modelle bis zu einer Textur (layer0, sonst all/top/side …) →
Fallback textures/item|block/<name>.png. Animierte Texturen sind senkrechte
Streifen; das Frontend zeigt nur das erste Bild.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import zipfile
from pathlib import Path

import httpx

from . import instances
from .config import settings

logger = logging.getLogger("mc-dashboard.items")

_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
_RESOURCES = "https://resources.download.minecraft.net"
_VERSION_RE = re.compile(r"^[A-Za-z0-9._\-]{1,40}$")
_ASSET_RE = re.compile(
    r"^assets/([a-z0-9_.\-]+)/(textures/(item|block)/.+\.png|models/(item|block)/.+\.json"
    r"|items/.+\.json|lang/(en_us|de_de)\.json)$")
_TEXTURE_KEYS = ("layer0", "all", "top", "side", "front", "texture", "end", "cross",
                 "particle")

_RETRY_SECONDS = 600
_vanilla_lock = threading.Lock()
_vanilla_jobs: dict = {}  # version → {"status": ..., "error": ...}


def cache_dir() -> Path:
    return settings.instances_dir / ".itemcache"


def _vanilla_zip(version: str) -> Path:
    return cache_dir() / f"vanilla-{version}.zip"


# ---------------------------------------------------------------------------
# Vanilla-Assets (Mojang)
# ---------------------------------------------------------------------------

def vanilla_status(version: str) -> str:
    """ready | loading | failed | disabled | unavailable"""
    if not settings.item_icons_vanilla:
        return "disabled"
    if not _VERSION_RE.match(version or ""):
        return "unavailable"
    if _vanilla_zip(version).is_file():
        return "ready"
    with _vanilla_lock:
        job = _vanilla_jobs.get(version)
    return job["status"] if job else "unavailable"


def ensure_vanilla(version: str) -> str:
    """Startet den Download im Hintergrund, falls nötig. Liefert den Status.
    Nach einem Fehlschlag frühestens nach _RETRY_SECONDS erneut."""
    status = vanilla_status(version)
    if status in ("ready", "disabled", "loading") or not _VERSION_RE.match(version or ""):
        return status
    with _vanilla_lock:
        job = _vanilla_jobs.get(version)
        if job and (job["status"] == "loading"
                    or time.time() - job.get("at", 0) < _RETRY_SECONDS):
            return job["status"]
        _vanilla_jobs[version] = {"status": "loading", "error": None, "at": time.time()}
    threading.Thread(target=_download_vanilla, args=(version,), daemon=True,
                     name=f"vanilla-assets-{version}").start()
    return "loading"


def _download_vanilla(version: str) -> None:
    target = _vanilla_zip(version)
    jar_tmp = target.with_suffix(".jar.part")
    zip_tmp = target.with_suffix(".zip.part")
    try:
        cache_dir().mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": settings.user_agent}
        with httpx.Client(timeout=60, follow_redirects=True, headers=headers) as client:
            manifest = client.get(_MANIFEST).raise_for_status().json()
            entry = next((v for v in manifest.get("versions") or []
                          if v.get("id") == version), None)
            if not entry:
                raise RuntimeError(f"Version {version} bei Mojang unbekannt")
            meta = client.get(entry["url"]).raise_for_status().json()
            with client.stream("GET", meta["downloads"]["client"]["url"]) as resp:
                resp.raise_for_status()
                with open(jar_tmp, "wb") as fh:
                    for chunk in resp.iter_bytes(1 << 16):
                        fh.write(chunk)
            de_lang = None
            try:
                index = client.get(meta["assetIndex"]["url"]).raise_for_status().json()
                obj = (index.get("objects") or {}).get("minecraft/lang/de_de.json")
                if obj and re.match(r"^[0-9a-f]{40}$", obj.get("hash", "")):
                    h = obj["hash"]
                    de_lang = client.get(f"{_RESOURCES}/{h[:2]}/{h}").raise_for_status().content
            except (httpx.HTTPError, ValueError, KeyError):
                de_lang = None  # deutsche Namen sind nett, aber kein Muss
        with zipfile.ZipFile(jar_tmp) as src, \
                zipfile.ZipFile(zip_tmp, "w", zipfile.ZIP_DEFLATED) as dst:
            for name in src.namelist():
                if name.startswith("assets/minecraft/") and _ASSET_RE.match(name):
                    dst.writestr(name, src.read(name))
            if de_lang:
                dst.writestr("assets/minecraft/lang/de_de.json", de_lang)
        zip_tmp.replace(target)
        with _vanilla_lock:
            _vanilla_jobs[version] = {"status": "ready", "error": None, "at": time.time()}
        logger.info("Vanilla-Item-Icons für %s geladen (%d KB)", version,
                    target.stat().st_size // 1024)
    except Exception as exc:
        with _vanilla_lock:
            _vanilla_jobs[version] = {"status": "failed", "error": str(exc), "at": time.time()}
        logger.warning("Vanilla-Item-Icons für %s nicht ladbar: %s", version, exc)
    finally:
        jar_tmp.unlink(missing_ok=True)
        zip_tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Index über alle Quellen einer Instanz
# ---------------------------------------------------------------------------

def _split_id(ref: str, default_ns: str = "minecraft") -> tuple:
    ref = ref.lstrip("#")
    if ":" in ref:
        ns, path = ref.split(":", 1)
        return ns, path
    return default_ns, ref


class ItemIndex:
    def __init__(self, sources: list) -> None:
        self._files: dict = {}     # "assets/…" → Pfad der Quelle
        self._zips: dict = {}
        self._lock = threading.Lock()
        self._icons: dict = {}
        self.items: set = set()
        self.names: dict = {}
        lang_en: dict = {}
        lang_de: dict = {}
        for source in sources:
            try:
                with zipfile.ZipFile(source) as zf:
                    for name in zf.namelist():
                        match = _ASSET_RE.match(name)
                        if not match:
                            continue
                        if name.endswith("/lang/en_us.json") or name.endswith("/lang/de_de.json"):
                            try:
                                data = json.loads(zf.read(name).decode("utf-8-sig"))
                            except (ValueError, UnicodeDecodeError, OSError):
                                continue
                            if isinstance(data, dict):
                                target = lang_de if name.endswith("de_de.json") else lang_en
                                for key, val in data.items():
                                    if isinstance(val, str):
                                        target.setdefault(key, val)
                            continue
                        # erste Quelle gewinnt (Vanilla vor Mods)
                        self._files.setdefault(name, source)
                        ns, rest = match.group(1), match.group(2)
                        if rest.startswith("models/item/"):
                            self.items.add(f"{ns}:{rest[len('models/item/'):-5]}")
                        elif rest.startswith("items/"):
                            self.items.add(f"{ns}:{rest[len('items/'):-5]}")
            except (OSError, zipfile.BadZipFile):
                continue
        # Verzauberungen: enchantment.<ns>.<name> → "<ns>:<name>"
        self.enchantments: dict = {}
        for lang in (lang_en, lang_de):  # Deutsch überschreibt Englisch
            for key, val in lang.items():
                parts = key.split(".")
                if len(parts) == 3 and parts[0] == "enchantment":
                    self.enchantments[f"{parts[1]}:{parts[2]}"] = val
        for item_id in self.items:
            ns, path = _split_id(item_id)
            dotted = path.replace("/", ".")
            for lang in (lang_de, lang_en):
                label = lang.get(f"item.{ns}.{dotted}") or lang.get(f"block.{ns}.{dotted}")
                if label:
                    self.names[item_id] = label
                    break

    def _read(self, member: str) -> bytes | None:
        source = self._files.get(member)
        if source is None:
            return None
        with self._lock:
            zf = self._zips.get(source)
            if zf is None:
                try:
                    zf = self._zips[source] = zipfile.ZipFile(source)
                except (OSError, zipfile.BadZipFile):
                    return None
            try:
                return zf.read(member)
            except (OSError, KeyError, zipfile.BadZipFile):
                return None

    def _json(self, member: str):
        raw = self._read(member)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _texture(self, ref: str) -> bytes | None:
        ns, path = _split_id(ref)
        return self._read(f"assets/{ns}/textures/{path}.png")

    @staticmethod
    def _model_ref(tree):
        """Erstes "model"-Feld in einer items/-Definition (ab 1.21.4)."""
        if isinstance(tree, dict):
            if isinstance(tree.get("model"), str):
                return tree["model"]
            for val in tree.values():
                found = ItemIndex._model_ref(val)
                if found:
                    return found
        elif isinstance(tree, list):
            for val in tree:
                found = ItemIndex._model_ref(val)
                if found:
                    return found
        return None

    def _model_texture(self, model_ref: str) -> bytes | None:
        textures: dict = {}
        ref = model_ref
        for _ in range(8):
            ns, path = _split_id(ref)
            if path in ("builtin/generated", "builtin/entity", "item/generated"):
                break
            model = self._json(f"assets/{ns}/models/{path}.json")
            if not isinstance(model, dict):
                break
            for key, val in (model.get("textures") or {}).items():
                if isinstance(val, str):
                    textures.setdefault(key, val)
            parent = model.get("parent")
            if not isinstance(parent, str):
                break
            ref = parent  # ohne Namespace gilt "minecraft:"
        keys = [k for k in _TEXTURE_KEYS if k in textures] + sorted(textures)
        for key in keys:
            val = textures[key]
            for _ in range(5):  # "#side" → Verweis auflösen
                if not val.startswith("#"):
                    break
                val = textures.get(val[1:], "")
            if val and not val.startswith("#"):
                data = self._texture(val)
                if data:
                    return data
        return None

    def icon(self, item_id: str) -> bytes | None:
        if item_id in self._icons:
            return self._icons[item_id]
        ns, path = _split_id(item_id)
        data = None
        definition = self._json(f"assets/{ns}/items/{path}.json")
        model_ref = self._model_ref(definition) if definition else None
        if model_ref:
            data = self._model_texture(model_ref)
        if data is None:
            data = self._model_texture(f"{ns}:item/{path}")
        if data is None:
            data = (self._texture(f"{ns}:item/{path}")
                    or self._texture(f"{ns}:block/{path}"))
        if len(self._icons) < 5000:
            self._icons[item_id] = data
        return data

    def search(self, query: str, limit: int = 30) -> list:
        q = (query or "").strip().lower()
        if not q:
            return []
        hits = []
        for item_id in self.items:
            name = self.names.get(item_id, "")
            low_name, low_id = name.lower(), item_id.lower()
            if q not in low_name and q not in low_id:
                continue
            path = low_id.split(":", 1)[-1]
            rank = 0 if (low_name.startswith(q) or path.startswith(q) or low_id == q) else 1
            hits.append((rank, low_id.startswith("minecraft:") is False, name or item_id, item_id))
        hits.sort()
        return [{"id": h[3], "name": self.names.get(h[3])} for h in hits[:limit]]

    def close(self) -> None:
        with self._lock:
            for zf in self._zips.values():
                try:
                    zf.close()
                except OSError:
                    pass
            self._zips.clear()


_index_lock = threading.Lock()
_build_locks: dict = {}
_indexes: dict = {}  # instance_id → (signature, ItemIndex)


def _sources(instance: dict) -> tuple:
    files = []
    vanilla = _vanilla_zip(instance.get("game_version") or "")
    if _VERSION_RE.match(instance.get("game_version") or "") and vanilla.is_file():
        files.append(vanilla)
    try:
        mods = sorted(p for p in instances.mods_dir(instance["id"]).glob("*.jar") if p.is_file())
    except OSError:
        mods = []
    files += mods
    signature = []
    for path in files:
        try:
            stat = path.stat()
            signature.append((path.name, stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue
    return files, tuple(signature)


def index_for(instance: dict) -> ItemIndex:
    """Index der Instanz (gecacht, neu gebaut wenn sich JARs ändern).
    Blockiert — aus async-Routen per asyncio.to_thread aufrufen."""
    inst_id = instance["id"]
    with _index_lock:
        build_lock = _build_locks.setdefault(inst_id, threading.Lock())
    with build_lock:
        files, signature = _sources(instance)
        cached = _indexes.get(inst_id)
        if cached and cached[0] == signature:
            return cached[1]
        index = ItemIndex(files)
        if cached:
            cached[1].close()
        _indexes[inst_id] = (signature, index)
        logger.info("Item-Index %s: %d Items aus %d Quellen", inst_id,
                    len(index.items), len(files))
        return index


def reset_for_tests() -> None:
    with _index_lock:
        for _sig, index in _indexes.values():
            index.close()
        _indexes.clear()
    with _vanilla_lock:
        _vanilla_jobs.clear()


__all__ = ["ItemIndex", "ensure_vanilla", "index_for", "vanilla_status"]
