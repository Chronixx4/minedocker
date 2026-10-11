"""Server-Update: beliebige Datei hochladen, Inhalt automatisch erkennen und
einen bestehenden Server damit aktualisieren.

Zwei Schritte (damit der Nutzer vor dem Eingriff sieht, was erkannt wurde):
  1. analyze_upload()  - Datei in die Zwischenablage legen, Inhalt anhand der
                         Dateiliste erkennen, Plan + Warnungen zurückgeben
  2. start_apply()     - erkannten Inhalt auf die (gestoppte) Instanz anwenden;
                         vorher immer ein Sicherheits-Snapshot (fail-closed)

Erkannte Arten (kind):
  modpack   .mrpack / CurseForge-.zip (modrinth.index.json / manifest.json)
  server    Server-Pack / kompletter Server-Ordner (mods, config, plugins, Welt …)
  world     nur eine Welt (level.dat)
  datapack  Datapack-.zip (pack.mcmeta)
  mods      einzelne .jar-Datei(en) bzw. .zip nur mit Jars (Mods oder Plugins)

Server-Software (Jars im Wurzelverzeichnis, libraries/, versions/ …),
server.properties, instance.json, eula.txt und Spielerlisten werden NIE aus dem
Upload übernommen - die Server-Software liefert der Container passend zu
Minecraft-Version/Loader der Instanz.
"""
import asyncio
import json
import logging
import re
import shutil
import tarfile
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import instances, modrinth, packs, worlds
from .config import ALLOWED_LOADERS, settings
from .security import validate_identifier

logger = logging.getLogger("minecraft-dashboard")

MAX_UPLOAD_BYTES = packs._MAX_PACK_BYTES
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_FILENAME_RE = re.compile(
    r'^(?!\.)[^/\\\x00-\x1f:*?"<>|]+\.(mrpack|zip|tar\.gz|jar)$', re.IGNORECASE)
# Mod-Dateinamen kommen aus dem Dateisystem (nur Basename), enthalten aber legitim
# Apostrophe, Kommas, Umlaute usw. ("L_Ender's Cataclysm ...jar"). Abgelehnt werden
# nur Pfadtrenner, Steuerzeichen, unter Windows verbotene Zeichen und führende Punkte.
_JAR_NAME_RE = re.compile(r'^(?!\.)[^/\\\x00-\x1f:*?"<>|]+\.jar$', re.IGNORECASE)
_STAGE_TTL = 3600          # Analyse-Ergebnisse verfallen nach 1 h
_MAX_DATAPACK_BYTES = 256 * 1024 * 1024

# Wird beim Server-Update aus dem Upload NICHT übernommen (Top-Level-Namen)
_SKIP_TOP = frozenset({
    "instance.json", "server.properties", "eula.txt", "libraries", "versions",
    "logs", "crash-reports", "backups", "packs", "cache", ".fabric", "bluemap",
    "_staging", "whitelist.json", "ops.json", "banned-players.json",
    "banned-ips.json", "usercache.json", "session.lock", ".git", "__MACOSX",
})
_SKIP_SUFFIXES = (".sh", ".bat", ".ps1", ".cmd", ".log", ".lock")
# Ordner, deren Inhalt als Mod-/Plugin-Jars versioniert wird
_JAR_DIRS = ("mods", "plugins")
_WORLD_COMPANIONS = ("_nether", "_the_end")


# ---------------------------------------------------------------------------
# Zwischenablage (pro Analyse ein Ordner + meta.json)
# ---------------------------------------------------------------------------

def _stage_root() -> Path:
    path = settings.instances_dir / "_staging" / "update"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _stage_path(token: str) -> Path:
    if not _TOKEN_RE.match(token or ""):
        raise HTTPException(status_code=400, detail="Ungültiges Update-Token")
    return _stage_root() / token


def _cleanup_stale() -> None:
    cutoff = time.time() - _STAGE_TTL
    try:
        for entry in _stage_root().iterdir():
            try:
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


def discard(token: str) -> None:
    shutil.rmtree(_stage_path(token), ignore_errors=True)


def validate_upload_filename(filename: str) -> str:
    name = filename or ""
    if (not name or len(name) > 255 or "/" in name or "\\" in name
            or ".." in name or "\x00" in name or not _FILENAME_RE.match(name)):
        raise HTTPException(
            status_code=400,
            detail="Erlaubt: .zip, .mrpack, .tar.gz oder .jar (einfacher Dateiname)")
    return name


def new_stage(filename: str) -> tuple:
    """(token, ziel_pfad) für einen frischen Upload."""
    validate_upload_filename(filename)
    _cleanup_stale()
    token = uuid.uuid4().hex
    folder = _stage_path(token)
    folder.mkdir(parents=True, exist_ok=True)
    return token, folder / filename


# ---------------------------------------------------------------------------
# Datei-Listen + Erkennung
# ---------------------------------------------------------------------------

def _list_names(path: Path, filename: str) -> list:
    """Alle Datei-Pfade (ohne Ordner-Einträge) des Archivs, '/'-normalisiert."""
    lower = filename.lower()
    try:
        if lower.endswith(".tar.gz"):
            with tarfile.open(path, "r:gz") as tf:
                return [m.name.lstrip("./") for m in tf.getmembers() if m.isfile()]
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > worlds._MAX_MEMBERS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Archiv enthält zu viele Dateien (max. {worlds._MAX_MEMBERS})")
            return [i.filename.replace("\\", "/") for i in infos if not i.is_dir()]
    except HTTPException:
        raise
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as exc:
        raise HTTPException(status_code=400,
                            detail=f"Archiv nicht lesbar: {exc}") from exc


def _strip_prefix(names: list) -> tuple:
    """Entfernt einen gemeinsamen Wrapper-Ordner ('Server/…'), falls sämtliche
    Einträge darin liegen. Rückgabe: (prefix, relative_namen)."""
    names = [n for n in names if n and not n.startswith("__MACOSX/")]
    tops = {n.split("/", 1)[0] for n in names if "/" in n}
    if names and all("/" in n for n in names) and len(tops) == 1:
        top = next(iter(tops))
        return top + "/", [n[len(top) + 1:] for n in names]
    return "", names


def _read_zip_text(path: Path, member: str, limit: int = 64 * 1024) -> str:
    try:
        with zipfile.ZipFile(path) as zf, zf.open(member) as fh:
            return fh.read(limit).decode("utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile, OSError):
        return ""


def _neoforge_mc(version: str) -> str | None:
    """NeoForge 21.1.x → 1.21.1, 21.0.x → 1.21, 20.4.x → 1.20.4."""
    parts = version.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return f"1.{parts[0]}" + (f".{parts[1]}" if parts[1] != "0" else "")


def _detect_software(names: list, vars_text: str = "") -> dict:
    """Loader / MC-Version / Loader-Version aus Bibliotheks-Pfaden, Dateinamen
    und (falls vorhanden) variables.txt eines Server-Packs ableiten."""
    loader = mc = loader_version = None

    def seg_after(prefix: str):
        for n in names:
            if n.startswith(prefix):
                rest = n[len(prefix):].split("/", 1)[0]
                if rest:
                    return rest
        return None

    forge = seg_after("libraries/net/minecraftforge/forge/")
    neo = seg_after("libraries/net/neoforged/neoforge/")
    fabric = seg_after("libraries/net/fabricmc/fabric-loader/")
    quilt = seg_after("libraries/org/quiltmc/quilt-loader/")
    if neo:
        loader, loader_version, mc = "neoforge", neo, _neoforge_mc(neo)
    elif forge:
        loader = "forge"
        m = re.match(r"^(\d+\.\d+(?:\.\d+)?)-(.+)$", forge)
        if m:
            mc, loader_version = m.group(1), m.group(2)
    elif fabric:
        loader, loader_version = "fabric", fabric
        mc = seg_after("libraries/net/fabricmc/intermediary/")
    elif quilt:
        loader, loader_version = "quilt", quilt
    mc = mc or seg_after("libraries/net/minecraft/server/")
    if not loader:
        flat = [n for n in names if "/" not in n]
        if any(n in ("config/paper-global.yml", "paper.yml", "paper-global.yml")
               for n in names) or any(n.startswith("paper-") for n in flat):
            loader = "paper"
        elif "spigot.yml" in names:
            loader = "spigot"
        elif "bukkit.yml" in names:
            loader = "bukkit"
        else:
            for n in flat:
                m = re.match(r"^forge-(\d+\.\d+(?:\.\d+)?)-([\d.]+).*\.jar$", n)
                if m:
                    loader, mc, loader_version = "forge", mc or m.group(1), m.group(2)
                    break
    if not mc:
        mc = seg_after("versions/")
    # variables.txt (ATM/FTB-Server-Packs) hat Vorrang - ist explizit
    if vars_text:
        def var(key):
            m = re.search(rf"^{key}\s*=\s*(\S+)", vars_text, re.MULTILINE)
            return m.group(1).strip("\"'") if m else None
        v_mc, v_loader, v_lv = var("MINECRAFT_VERSION"), var("MODLOADER"), var("MODLOADER_VERSION")
        if v_mc:
            mc = v_mc
        if v_loader and v_loader.lower() in ALLOWED_LOADERS:
            loader = v_loader.lower()
        if v_lv:
            loader_version = v_lv
    if mc and not re.match(r"^\d+\.\d+(\.\d+)?$", mc):
        mc = None
    return {"loader": loader, "game_version": mc, "loader_version": loader_version}


def _inspect_jar(path: Path) -> dict | None:
    """Erkennt Mod bzw. Plugin anhand der Jar-Metadaten (None = unbekannt)."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return None
    if "fabric.mod.json" in names:
        return {"type": "mod", "loader": "fabric"}
    if "quilt.mod.json" in names:
        return {"type": "mod", "loader": "quilt"}
    if "META-INF/neoforge.mods.toml" in names:
        return {"type": "mod", "loader": "neoforge"}
    if "META-INF/mods.toml" in names or "mcmod.info" in names:
        return {"type": "mod", "loader": "forge"}
    if "paper-plugin.yml" in names or "plugin.yml" in names:
        return {"type": "plugin", "loader": None}
    return None


def _classify(names: list, filename: str) -> dict:
    """Ordnet die (präfix-bereinigte) Dateiliste einer Art zu."""
    top = {n.split("/", 1)[0] for n in names}
    root_files = [n for n in names if "/" not in n]
    root_jars = [n for n in root_files if n.lower().endswith(".jar")]
    mods = [n for n in names if n.startswith("mods/") and n.lower().endswith(".jar")]
    plugins = [n for n in names if n.startswith("plugins/") and n.lower().endswith(".jar")]
    level_files = [n for n in names if n == "level.dat" or n.endswith("/level.dat")]
    world_dirs = sorted({n.rsplit("/", 1)[0] for n in level_files if "/" in n
                         and n.count("/") == 1})
    has_root_world = "level.dat" in names
    server_markers = {"server.properties", "libraries", "eula.txt", "config",
                      "defaultconfigs", "kubejs", "mods", "plugins", "scripts",
                      "resourcepacks", "shaderpacks", "datapacks"}
    return {"top": top, "root_jars": root_jars, "mods": mods, "plugins": plugins,
            "has_root_world": has_root_world, "world_dirs": world_dirs,
            "has_world": has_root_world or bool(world_dirs),
            "markers": top & server_markers}


def analyze_upload(instance_id: str, token: str, path: Path, filename: str) -> dict:
    """Erkennt den Inhalt des hochgeladenen Files und baut den Update-Plan.

    Wirft HTTPException (400) bei nicht erkennbarem Inhalt."""
    instance = instances.get_instance(instance_id)
    lower = filename.lower()
    size = path.stat().st_size
    result: dict[str, Any] = {"token": token, "filename": filename, "size": size,
              "instance_id": instance_id, "warnings": [], "plan": [],
              "options": {}, "detected": {}}
    meta = {"instance_id": instance_id, "filename": filename, "created": time.time()}

    # --- einzelne Jar ------------------------------------------------------
    if lower.endswith(".jar"):
        info = _inspect_jar(path)
        if info is None:
            raise HTTPException(
                status_code=400,
                detail=("Diese .jar ist weder Mod noch Plugin. Die Server-Software "
                        "(Vanilla/Forge/Paper …) stellt der Container selbst bereit — "
                        "Version/Loader dazu in den Einstellungen ändern oder ein "
                        "Modpack/Server-Pack hochladen."))
        meta.update(kind="mods", jars=[filename], jar_type=info["type"])
        result.update(kind="mods", label="Einzelne Mod" if info["type"] == "mod"
                      else "Einzelnes Plugin")
        target = "plugins" if info["type"] == "plugin" else "mods"
        result["plan"].append(f"{filename} → {target}/ (ältere Version derselben "
                              f"Mod wird ersetzt)")
        _loader_warning(result, instance, info.get("loader"))
        return _finish(result, meta, path)

    # --- Modpack (Index-Datei) ---------------------------------------------
    raw_names = _list_names(path, filename)
    prefix, names = _strip_prefix(raw_names)
    is_tar = lower.endswith(".tar.gz")
    if not is_tar and ("modrinth.index.json" in raw_names or "manifest.json" in raw_names):
        fmt, raw_index = packs._extract_index(path)
        pack = packs._normalize_index(fmt, raw_index)
        meta["kind"] = "modpack"
        result.update(kind="modpack",
                      label=("Modrinth-Modpack" if fmt == "modrinth"
                             else "CurseForge-Modpack"))
        result["detected"] = {"title": pack.get("title"), "loader": pack.get("loader"),
                              "game_version": pack.get("game_version"),
                              "loader_version": pack.get("loader_version"),
                              "files": len(pack.get("files") or [])}
        result["plan"].append(
            f"Modpack „{pack.get('title') or filename}“ installieren "
            f"({len(pack.get('files') or [])} Mod-Dateien werden geladen)")
        _version_plan(result, instance, pack)
        existing = instance.get("modpack")
        if existing:
            result["plan"].append(
                f"Bisheriges Modpack „{existing.get('title')}“ wird überschrieben")
        result["warnings"].append(
            "Mods werden beim Installieren aus dem Internet nachgeladen.")
        return _finish(result, meta, path)

    cls = _classify(names, filename)

    # --- Datapack ----------------------------------------------------------
    if "pack.mcmeta" in names and not cls["markers"] and not cls["has_world"]:
        meta["kind"] = "datapack"
        result.update(kind="datapack", label="Datapack")
        result["plan"].append(f"{filename} → Welt-Ordner datapacks/ (aktiv nach Start)")
        return _finish(result, meta, path)

    # --- nur Jars ----------------------------------------------------------
    only_jars = (bool(cls["mods"] or cls["root_jars"] or cls["plugins"])
                 and not cls["has_world"]
                 and not (cls["markers"] - {"mods", "plugins"})
                 and all(n.lower().endswith(".jar") for n in names))
    if only_jars:
        jars = [n for n in names if n.lower().endswith(".jar")]
        meta.update(kind="mods", jars=jars, jar_type=None)
        result.update(kind="mods", label=f"{len(jars)} Mod-/Plugin-Datei(en)")
        result["plan"].append(
            f"{len(jars)} Jar-Datei(en) installieren; ältere Versionen "
            f"derselben Mods werden ersetzt")
        return _finish(result, meta, path)

    # --- nur Welt ----------------------------------------------------------
    if cls["has_world"] and not (cls["markers"] - {"datapacks"}):
        meta["kind"] = "world"
        result.update(kind="world", label="Welt")
        result["plan"].append("Aktuelle Welt wird durch die hochgeladene ersetzt "
                              "(Sicherheits-Snapshot inklusive Welt vorab)")
        result["warnings"].append("Die bestehende Welt wird ersetzt.")
        return _finish(result, meta, path)

    # --- Server-Pack / kompletter Server-Ordner ----------------------------
    if cls["markers"] or cls["has_world"] or cls["root_jars"]:
        vars_text = ""
        if not is_tar and "variables.txt" in names:
            vars_text = _read_zip_text(path, prefix + "variables.txt")
        det = _detect_software(names, vars_text)
        meta.update(kind="server", prefix=prefix)
        result.update(kind="server", label="Server-Pack / Server-Ordner")
        result["detected"] = det
        parts = []
        if cls["mods"]:
            parts.append(f"{len(cls['mods'])} Mods")
        if cls["plugins"]:
            parts.append(f"{len(cls['plugins'])} Plugins")
        for folder, label in (("config", "Configs"), ("defaultconfigs", "Default-Configs"),
                              ("kubejs", "KubeJS"), ("scripts", "Scripts"),
                              ("resourcepacks", "Resourcepacks"),
                              ("shaderpacks", "Shaderpacks")):
            if folder in cls["top"]:
                parts.append(label)
        result["plan"].append("Wird aktualisiert: " + (", ".join(parts) or "Dateien"))
        if cls["mods"]:
            result["options"]["clean_mods"] = True
            result["plan"].append("mods/ wird ersetzt (alte Mod-Versionen werden entfernt)")
        if "config" in cls["top"] or "defaultconfigs" in cls["top"]:
            result["options"]["overwrite_config"] = True
        if cls["has_world"]:
            result["options"]["replace_world"] = False
            result["warnings"].append(
                "Der Upload enthält auch eine Welt — sie wird nur ersetzt, wenn du "
                "das unten ausdrücklich wählst.")
        skipped = sorted(t for t in cls["top"] if t in _SKIP_TOP)
        if skipped or cls["root_jars"]:
            result["plan"].append(
                "Nicht übernommen (der Container verwaltet das): Server-Software, "
                "server.properties, Spielerlisten, Start-Skripte")
        if det.get("game_version") or det.get("loader"):
            _version_plan(result, instance, det)
        else:
            result["warnings"].append(
                "Minecraft-Version/Loader sind aus dem Upload nicht erkennbar — "
                "die Server-Version bleibt unverändert.")
        return _finish(result, meta, path)

    raise HTTPException(
        status_code=400,
        detail=("Inhalt nicht erkannt. Unterstützt: Modpack (.mrpack/.zip), "
                "Server-Pack/Server-Ordner, Welt, Datapack oder Mod-/Plugin-Jars."))


def _finish(result: dict, meta: dict, path: Path) -> dict:
    (path.parent / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    result["requires_stop"] = True
    result["warnings"].append(
        "Vor dem Update wird automatisch ein Sicherheits-Snapshot angelegt.")
    return result


def _loader_warning(result: dict, instance: dict, loader) -> None:
    if loader and instance.get("loader") != loader and not (
            instance.get("loader") in ("fabric", "quilt") and loader == "fabric"):
        result["warnings"].append(
            f"Diese Datei ist für {loader}, die Instanz nutzt {instance.get('loader')} "
            f"— der Server startet damit evtl. nicht.")


def _version_plan(result: dict, instance: dict, wanted: dict) -> None:
    """Plan-/Warnzeilen zur Minecraft-/Loader-Umstellung (nur bei Abweichung)."""
    loader = (wanted.get("loader") or "").lower() or None
    game = wanted.get("game_version")
    changes = []
    if game and game != instance.get("game_version"):
        changes.append(f"Minecraft {instance.get('game_version')} → {game}")
    if loader and loader != instance.get("loader"):
        changes.append(f"Loader {instance.get('loader')} → {loader}")
    if loader and loader not in ALLOWED_LOADERS:
        result["warnings"].append(
            f"Loader „{loader}“ wird für Server nicht unterstützt.")
    if changes:
        result["plan"].append("Server wird umgestellt: " + ", ".join(changes))
        result["version_change"] = {
            "from": {"loader": instance.get("loader"),
                     "game_version": instance.get("game_version")},
            "to": {"loader": loader or instance.get("loader"),
                   "game_version": game or instance.get("game_version")}}
        result["warnings"].append(
            "Ein Wechsel der Minecraft-Version kann Welten und Mods inkompatibel "
            "machen — der Snapshot erlaubt das Zurückrollen.")
    else:
        result["plan"].append("Minecraft-Version und Loader passen bereits")


# ---------------------------------------------------------------------------
# Anwenden
# ---------------------------------------------------------------------------

def load_stage(instance_id: str, token: str) -> tuple:
    """(meta, archiv_pfad) einer früheren Analyse; 404 wenn abgelaufen."""
    folder = _stage_path(token)
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(status_code=404,
                            detail="Analyse abgelaufen — Datei bitte erneut hochladen")
    if meta.get("instance_id") != instance_id:
        raise HTTPException(status_code=404, detail="Analyse gehört zu einer anderen Instanz")
    archive = folder / validate_upload_filename(meta.get("filename", ""))
    if not archive.is_file():
        raise HTTPException(status_code=404,
                            detail="Analyse abgelaufen — Datei bitte erneut hochladen")
    return meta, archive


def _mod_key(filename: str) -> str:
    """Grober Mod-Schlüssel ohne Versionsteil: 'sodium-fabric-0.5.8+mc1.21.jar' →
    'sodium-fabric'. Dient dem Erkennen älterer Versionen derselben Mod."""
    base = re.sub(r"\.jar(\.disabled)?$", "", filename, flags=re.IGNORECASE).lower()
    m = re.match(r"^(.*?)(?:[-_+ ]+(?:mc)?v?\d.*)?$", base)
    return (m.group(1) if m and m.group(1) else base)


def _install_jars(jars: list, target: Path, replace_older: bool) -> tuple:
    """Kopiert (quelle, name)-Paare nach target; entfernt ältere Versionen
    derselben Mod. Rückgabe: (installiert, ersetzt, übersprungen)."""
    target.mkdir(parents=True, exist_ok=True)
    installed, replaced, skipped = [], [], []
    existing = [p for p in target.iterdir()
                if p.is_file() and re.search(r"\.jar(\.disabled)?$", p.name, re.IGNORECASE)]
    for src, name in jars:
        if not _JAR_NAME_RE.match(name):
            skipped.append(f"{name} (ungültiger Dateiname)")
            continue
        if replace_older:
            key = _mod_key(name)
            for old in list(existing):
                if old.name != name and _mod_key(old.name) == key:
                    old.unlink(missing_ok=True)
                    existing.remove(old)
                    replaced.append(old.name)
        shutil.copy2(src, target / name)
        installed.append(name)
    return installed, replaced, skipped


def _copy_tree(src: Path, dest: Path, overwrite: bool, counter: dict) -> None:
    """Rekursives Kopieren ohne Symlinks; overwrite=False lässt Vorhandenes stehen."""
    dest.mkdir(parents=True, exist_ok=True)
    for child in sorted(src.iterdir()):
        if child.is_symlink():
            continue
        target = dest / child.name
        if child.is_dir():
            _copy_tree(child, target, overwrite, counter)
        elif target.exists() and not overwrite:
            counter["kept"] += 1
        else:
            shutil.copy2(child, target)
            counter["files"] += 1


def _find_root(extracted: Path, prefix: str) -> Path:
    root = extracted / prefix.rstrip("/") if prefix else extracted
    return root if root.is_dir() else extracted


def _apply_version(instance_id: str, det: dict, adapt: bool) -> dict | None:
    """Stellt Loader/MC-Version der Instanz um (nur wenn erkannt + abweichend)."""
    if not adapt or not det:
        return None
    instance = instances.get_instance(instance_id)
    loader = (det.get("loader") or "").lower() or None
    game = det.get("game_version")
    if loader and loader not in ALLOWED_LOADERS:
        raise HTTPException(status_code=400,
                            detail=f"Loader „{loader}“ wird für Server nicht unterstützt")
    before = {"loader": instance.get("loader"), "game_version": instance.get("game_version")}
    if game:
        instance["game_version"] = validate_identifier(game, "Minecraft-Version")
    if loader:
        instance["loader"] = loader
    if det.get("loader_version"):
        instance["loader_version"] = str(det["loader_version"])
    after = {"loader": instance.get("loader"), "game_version": instance.get("game_version")}
    instances.update_instance(instance)
    return {"from": before, "to": after} if before != after else None


def _remember(instance_id: str, kind: str, filename: str, summary: dict) -> None:
    instance = instances.get_instance(instance_id)
    instance["last_update"] = {"at": int(time.time()), "kind": kind,
                               "filename": filename, "summary": summary}
    instances.update_instance(instance)


def _apply_sync(job: dict, instance_id: str, meta: dict, archive: Path,
                options: dict) -> dict:
    """Blockierender Teil des Updates (läuft im Thread)."""
    from . import backups  # lazy, vermeidet Import-Zirkel
    kind = meta["kind"]
    filename = meta["filename"]
    directory = instances.instance_dir(instance_id)

    job["phase"] = "Sicherheits-Snapshot"
    snap_kind = "pre-restore" if (
        kind == "world" or (kind == "server" and options.get("replace_world"))
    ) else "pre-install"
    backups.safety_backup(instance_id, directory, snap_kind)

    summary: dict = {"kind": kind}
    lower = filename.lower()

    if kind == "world":
        job["phase"] = "Welt ersetzen"
        out = worlds.restore_world_upload(instance_id, archive, filename)
        summary["world_dir"] = out.get("world_dir")

    elif kind == "datapack":
        job["phase"] = "Datapack installieren"
        size = archive.stat().st_size
        if size > _MAX_DATAPACK_BYTES:
            raise HTTPException(status_code=413, detail="Datapack zu groß (max. 256 MiB)")
        from . import datapacks
        out = datapacks.upload_datapack(instance_id, filename, archive.read_bytes())
        summary["datapack"] = out["name"]

    elif kind == "mods":
        job["phase"] = "Mods installieren"
        instance = instances.get_instance(instance_id)
        use_plugins = (meta.get("jar_type") == "plugin"
                       or instance.get("loader") in ("paper", "spigot", "bukkit"))
        target = directory / ("plugins" if use_plugins else "mods")
        pairs = []
        tmp = Path(tempfile.mkdtemp(prefix="upd_jars_", dir=worlds.staging_dir()))
        try:
            if lower.endswith(".jar"):
                pairs.append((archive, filename))
            else:
                worlds._extract_archive(archive, tmp, filename)
                for jar in sorted(tmp.rglob("*.jar")):
                    if jar.is_file() and not jar.is_symlink():
                        pairs.append((jar, jar.name))
            installed, replaced, skipped = _install_jars(pairs, target, True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        summary.update(installed=installed, replaced=replaced, skipped=skipped,
                       target=target.name)

    elif kind == "server":
        tmp = Path(tempfile.mkdtemp(prefix="upd_srv_", dir=worlds.staging_dir()))
        try:
            job["phase"] = "Archiv entpacken"
            worlds._extract_archive(archive, tmp, filename)
            root = _find_root(tmp, meta.get("prefix", ""))
            summary.update(_apply_server(job, instance_id, root, directory, options))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if options.get("adapt_version", True):
            job["phase"] = "Version umstellen"
            det = options.get("_detected") or {}
            adapted = _apply_version(instance_id, det, True)
            if adapted:
                summary["version_adapted"] = adapted
    else:
        raise HTTPException(status_code=400, detail=f"Unbekannte Art: {kind}")

    _remember(instance_id, kind, filename, {k: v for k, v in summary.items()
                                            if k != "kind"})
    return summary


def _apply_server(job: dict, instance_id: str, root: Path, directory: Path,
                  options: dict) -> dict:
    clean_mods = bool(options.get("clean_mods", True))
    overwrite_cfg = bool(options.get("overwrite_config", True))
    replace_world = bool(options.get("replace_world", False))
    counter = {"files": 0, "kept": 0}
    summary: dict = {}
    replaced_jars: list = []

    # Welt-Ordner des Uploads erkennen (level.dat am Wurzel oder in Unterordnern)
    world_src = None
    if (root / "level.dat").is_file():
        world_src = root
    else:
        for sub in sorted(root.iterdir()):
            if sub.is_dir() and not sub.is_symlink() and (sub / "level.dat").is_file():
                world_src = sub
                break
    world_names = set()
    if world_src is not None and world_src != root:
        world_names.add(world_src.name)
        for suffix in _WORLD_COMPANIONS:
            world_names.add(world_src.name + suffix)

    for child in sorted(root.iterdir()):
        name = child.name
        if child.is_symlink() or name in _SKIP_TOP:
            continue
        if name in world_names:
            continue  # Welt-Ordner separat unten
        if child.is_file():
            if name.lower().endswith(_SKIP_SUFFIXES) or name.lower().endswith(".jar"):
                continue  # Start-Skripte + Server-Software
            if world_src is root and name in worlds._WORLD_ITEMS:
                continue
            job["phase"] = f"Kopiere {name}"
            shutil.copy2(child, directory / name)
            counter["files"] += 1
            continue
        if world_src is root and name in worlds._WORLD_ITEMS:
            continue
        job["phase"] = f"Aktualisiere {name}/"
        if name in _JAR_DIRS:
            jars = [(p, p.name) for p in sorted(child.glob("*.jar")) if p.is_file()]
            target = directory / name
            if clean_mods and target.is_dir():
                for old in target.iterdir():
                    if old.is_file() and re.search(r"\.jar(\.disabled)?$", old.name,
                                                   re.IGNORECASE):
                        old.unlink(missing_ok=True)
                        replaced_jars.append(old.name)
            installed, replaced, skipped = _install_jars(jars, target, not clean_mods)
            replaced_jars.extend(replaced)
            summary[f"{name}_installed"] = len(installed)
            if skipped:
                summary.setdefault("skipped", []).extend(skipped)
            # Rest des Ordners (z. B. plugins/<Plugin>/config.yml) übernehmen
            for sub in sorted(child.iterdir()):
                if sub.is_dir() and not sub.is_symlink():
                    _copy_tree(sub, target / sub.name, overwrite_cfg, counter)
                elif sub.is_file() and not sub.name.lower().endswith(".jar"):
                    dst = target / sub.name
                    if overwrite_cfg or not dst.exists():
                        shutil.copy2(sub, dst)
                        counter["files"] += 1
        else:
            overwrite = overwrite_cfg or name not in ("config", "defaultconfigs")
            _copy_tree(child, directory / name, overwrite, counter)

    if replaced_jars:
        summary["removed_old"] = sorted(set(replaced_jars))[:200]
    if world_src is not None and replace_world:
        job["phase"] = "Welt ersetzen"
        previous = instances.find_world_dir(directory)
        target_name = world_src.name if world_src != root else (
            previous.name if previous else "world")
        if previous is not None and previous.name != target_name:
            shutil.rmtree(previous, ignore_errors=True)
        target = directory / target_name
        worlds._clear_dir(target)
        if world_src == root:
            target.mkdir(exist_ok=True)
            for item in worlds._WORLD_ITEMS:
                if (root / item).exists():
                    dst = target / item
                    if (root / item).is_dir():
                        _copy_tree(root / item, dst, True, counter)
                    else:
                        shutil.copy2(root / item, dst)
        else:
            _copy_tree(world_src, target, True, counter)
            for suffix in _WORLD_COMPANIONS:
                comp = root / (world_src.name + suffix)
                if comp.is_dir():
                    dst = directory / comp.name
                    worlds._clear_dir(dst)
                    _copy_tree(comp, dst, True, counter)
        worlds._patch_properties(instance_id, {"level-name": target.name})
        summary["world_replaced"] = target.name
    elif world_src is not None:
        summary["world_kept"] = True
    summary["files"] = counter["files"]
    if counter["kept"]:
        summary["kept_existing"] = counter["kept"]
    return summary


async def start_apply(instance_id: str, token: str, options: dict) -> dict:
    """Wendet eine zuvor analysierte Datei auf die Instanz an → Job-Dict."""
    from . import runtime  # lazy, vermeidet Import-Zirkel
    meta, archive = load_stage(instance_id, token)
    instance = instances.get_instance(instance_id)
    if runtime.is_running(instance):
        raise HTTPException(status_code=409,
                            detail="Instanz läuft — für das Update bitte zuerst stoppen")
    active = packs._running_pack_job(instance_id)
    if active:
        raise HTTPException(status_code=409,
                            detail=f"Es läuft bereits eine Installation "
                                   f"({active.get('filename')}) für diese Instanz")
    kind = meta["kind"]
    filename = meta["filename"]

    if kind == "modpack":
        # Bestehende, getestete Modpack-Pipeline (Auto-Version, Snapshot, Job)
        dest_dir = instances.pack_dir(instance_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        pack_name = instances.validate_pack_filename(filename)
        dest = dest_dir / pack_name
        shutil.move(str(archive), str(dest))
        try:
            job = await packs.install_upload(instance_id, dest, pack_name,
                                             force=True, auto_version=True)
        except Exception:
            dest.unlink(missing_ok=True)
            raise
        discard(token)
        return job

    if kind == "server":
        # Erkannte Version beim Anwenden erneut aus der Analyse ableiten
        names = _list_names(archive, filename)
        prefix, rel = _strip_prefix(names)
        vars_text = (_read_zip_text(archive, prefix + "variables.txt")
                     if not filename.lower().endswith(".tar.gz")
                     and "variables.txt" in rel else "")
        options = dict(options)
        options["_detected"] = _detect_software(rel, vars_text)

    size = archive.stat().st_size
    packs._check_disk_space(instances.instance_dir(instance_id), size * 3)
    job = modrinth.create_job(filename, 0, kind="update", phase="Update wird vorbereitet",
                              instance_id=instance_id, source="update")
    job["status"] = "installing"
    modrinth.track_task(asyncio.create_task(
        _run_apply(job, instance_id, token, meta, archive, options)))
    return job


async def _run_apply(job: dict, instance_id: str, token: str, meta: dict,
                     archive: Path, options: dict) -> None:
    try:
        summary = await asyncio.to_thread(_apply_sync, job, instance_id, meta,
                                          archive, options)
        job["summary"] = summary
        job["status"] = "done"
        job["phase"] = "Fertig"
        logger.info("Server-Update (%s) angewendet: %s → %s", meta["kind"],
                    meta["filename"], instance_id)
    except HTTPException as exc:
        job["status"] = "error"
        job["error"] = str(exc.detail)
        job["phase"] = "Fehlgeschlagen"
    except Exception as exc:  # Job muss immer terminieren
        logger.exception("Server-Update fehlgeschlagen")
        job["status"] = "error"
        job["error"] = str(exc) or exc.__class__.__name__
        job["phase"] = "Fehlgeschlagen"
    finally:
        shutil.rmtree(_stage_path(token), ignore_errors=True)
        modrinth.persist_job(job)

