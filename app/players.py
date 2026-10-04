"""Spieler-Übersicht: alle bekannten Spieler einer Instanz, Profil mit
Statistiken, Rekorden und Fortschritten, Live-Tabelle der Verbundenen.

Quellen (alles Dateien des Servers, funktioniert auch bei gestoppter Instanz):
- usercache.json, ops.json, whitelist.json, banned-players.json → Namen,
  UUIDs, Rollen.
- <welt>/stats/<uuid>.json → Statistiken (Kills, Tode, Strecken, Abbau …).
- <welt>/advancements/<uuid>.json → Fortschritte (ohne Rezepte).
- <welt>/playerdata/<uuid>.dat → Leben, Hunger, Level, Position, Bett (NBT).
- Dashboard-Verlauf (history.py) → Sitzungen und Spielzeit.

Der Server schreibt diese Dateien beim Autosave (Standard alle 5 Minuten)
und beim Ausloggen — für Online-Spieler liefert die Live-Abfrage per RCON
die aktuellen Werte.
"""
from __future__ import annotations

import json
import logging
import re
import time
import zipfile
from pathlib import Path

from fastapi import HTTPException

from . import history, instances, itemassets, nbt
from . import inventory as inventory_mod

logger = logging.getLogger("dashboard.players")

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_MAX_JSON = 4 * 1024 * 1024
_MAX_PLAYERS = 2000

# Felder für die Live-Abfrage per RCON ('data get entity <name> <feld>')
LIVE_FIELDS = ("Health", "foodLevel", "foodSaturationLevel", "XpLevel", "XpP",
               "Pos", "Dimension", "playerGameType", "Rotation",
               "respawn", "SpawnX", "SpawnY", "SpawnZ", "SpawnDimension")
GAMEMODES = {0: "survival", 1: "creative", 2: "adventure", 3: "spectator"}


def _read_json(path: Path):
    try:
        if not path.is_file() or path.stat().st_size > _MAX_JSON:
            return None
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _entries(path: Path) -> list:
    data = _read_json(path)
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _uuid(value) -> str | None:
    text = str(value or "").strip().lower()
    return text if UUID_RE.match(text) else None


def _uuid_files(directory: Path | None, suffix: str) -> set:
    if directory is None or not directory.is_dir():
        return set()
    out = set()
    try:
        for entry in directory.iterdir():
            if entry.name.endswith(suffix):
                uid = _uuid(entry.name[:-len(suffix)])
                if uid:
                    out.add(uid)
    except OSError:
        pass
    return out


# ---------------------------------------------------------------------------
# Statistiken
# ---------------------------------------------------------------------------

def _stat(stats: dict, group: str, key: str) -> int:
    value = (stats.get(f"minecraft:{group}") or {}).get(f"minecraft:{key}", 0)
    return int(value) if isinstance(value, (int, float)) else 0


_TRAVEL = ("walk_one_cm", "sprint_one_cm", "crouch_one_cm", "swim_one_cm",
           "walk_on_water_one_cm", "walk_under_water_one_cm", "climb_one_cm",
           "fall_one_cm", "fly_one_cm", "aviate_one_cm", "boat_one_cm",
           "horse_one_cm", "minecart_one_cm", "pig_one_cm", "strider_one_cm",
           "happy_ghast_one_cm")


def summarize_stats(raw) -> dict:
    """stats/<uuid>.json → Kennzahlen (Zeiten in Sekunden, Strecken in Metern)."""
    stats = (raw or {}).get("stats") if isinstance(raw, dict) else None
    if not isinstance(stats, dict):
        return {}
    ticks = _stat(stats, "custom", "play_time") or _stat(stats, "custom", "play_one_minute")
    mined = stats.get("minecraft:mined") or {}
    diamonds = sum(int(v) for k, v in mined.items()
                   if k in ("minecraft:diamond_ore", "minecraft:deepslate_diamond_ore")
                   and isinstance(v, (int, float)))
    travel = {key: _stat(stats, "custom", key) // 100 for key in _TRAVEL}
    killed = stats.get("minecraft:killed") or {}
    top_kills = sorted(((k.split(":", 1)[-1], int(v)) for k, v in killed.items()
                        if isinstance(v, (int, float))), key=lambda kv: -kv[1])[:5]
    return {
        "play_seconds": ticks // 20,
        "deaths": _stat(stats, "custom", "deaths"),
        "mob_kills": _stat(stats, "custom", "mob_kills"),
        "player_kills": _stat(stats, "custom", "player_kills"),
        "since_death_seconds": _stat(stats, "custom", "time_since_death") // 20,
        "jumps": _stat(stats, "custom", "jump"),
        "damage_dealt": _stat(stats, "custom", "damage_dealt") // 10,
        "damage_taken": _stat(stats, "custom", "damage_taken") // 10,
        "diamonds": diamonds,
        "blocks_mined": sum(int(v) for v in mined.values() if isinstance(v, (int, float))),
        "distance_m": sum(travel.values()),
        "travel_m": {k.removesuffix("_one_cm"): v for k, v in travel.items() if v},
        "top_kills": [{"mob": k, "count": v} for k, v in top_kills],
        "sleeps": _stat(stats, "custom", "sleep_in_bed"),
        "fish": _stat(stats, "custom", "fish_caught"),
    }


def summarize_advancements(raw) -> list:
    """advancements/<uuid>.json → erledigte Fortschritte (ohne Rezepte), neueste zuerst."""
    if not isinstance(raw, dict):
        return []
    done = []
    for key, entry in raw.items():
        if not isinstance(entry, dict) or not entry.get("done"):
            continue
        ns, _, path = key.partition(":")
        if not path or path.startswith("recipes/"):
            continue
        stamps = [str(v) for v in (entry.get("criteria") or {}).values()]
        done.append({"id": key, "group": path.split("/", 1)[0],
                     "path": path, "ns": ns, "at": max(stamps, default=None)})
    done.sort(key=lambda a: a["at"] or "", reverse=True)
    return done


def advancement_names(game_version: str) -> tuple:
    """(Titel je Fortschritt, Anzahl Vanilla-Fortschritte) aus dem
    Vanilla-Asset-Cache der Inventar-Ansicht; ({}, None) ohne Cache."""
    path = itemassets.cache_dir() / f"vanilla-{game_version}.zip"
    titles: dict = {}
    if itemassets.vanilla_status(game_version) != "ready" or not path.is_file():
        return titles, None
    try:
        with zipfile.ZipFile(path) as zf:
            for lang in ("en_us", "de_de"):  # Deutsch gewinnt
                try:
                    data = json.loads(zf.read(f"assets/minecraft/lang/{lang}.json"))
                except KeyError:
                    continue
                for key, val in data.items():
                    match = re.match(r"^advancements\.(\w+)\.(\w+)\.title$", key)
                    if match and isinstance(val, str):
                        titles[f"minecraft:{match.group(1)}/{match.group(2)}"] = val
    except (OSError, zipfile.BadZipFile, ValueError):
        return {}, None
    # Wurzeln ("story/root") zählt das Spiel nicht als eigenen Fortschritt
    total = sum(1 for k in titles if not k.endswith("/root"))
    return titles, total or None


# ---------------------------------------------------------------------------
# Spielerdaten (NBT)
# ---------------------------------------------------------------------------

def _round_pos(value) -> list | None:
    if isinstance(value, list) and len(value) == 3:
        try:
            return [round(float(v)) for v in value]
        except (TypeError, ValueError):
            return None
    return None


def vitals(data: dict) -> dict:
    """Werte aus playerdata-NBT oder Live-RCON (gleiches Feldformat)."""
    def num(key, default=None):
        value = data.get(key)
        return value if isinstance(value, (int, float)) else default

    bed = None
    respawn = data.get("respawn")
    if isinstance(respawn, dict):  # ab 1.21.5
        bed = {"pos": _round_pos(respawn.get("pos")),
               "dimension": respawn.get("dimension") or "minecraft:overworld"}
    elif num("SpawnX") is not None:
        bed = {"pos": [int(num("SpawnX")), int(num("SpawnY", 0)), int(num("SpawnZ", 0))],
               "dimension": data.get("SpawnDimension") or "minecraft:overworld"}
    rotation = data.get("Rotation")
    yaw = rotation[0] if isinstance(rotation, list) and rotation else None
    mode = num("playerGameType")
    return {
        "health": round(float(num("Health", 0)), 1) if num("Health") is not None else None,
        "food": int(num("foodLevel")) if num("foodLevel") is not None else None,
        "saturation": round(float(num("foodSaturationLevel", 0)), 1)
        if num("foodSaturationLevel") is not None else None,
        "level": int(num("XpLevel")) if num("XpLevel") is not None else None,
        "xp_progress": round(float(num("XpP", 0)), 2) if num("XpP") is not None else None,
        "pos": _round_pos(data.get("Pos")),
        "dimension": data.get("Dimension") if isinstance(data.get("Dimension"), str) else None,
        "gamemode": GAMEMODES.get(int(mode)) if mode is not None else None,
        "facing": facing(yaw),
        "bed": bed,
    }


def facing(yaw) -> str | None:
    """Minecraft-Yaw (0 = Süden, 90 = Westen) → Himmelsrichtung."""
    if not isinstance(yaw, (int, float)):
        return None
    names = ["S", "SW", "W", "NW", "N", "NO", "O", "SO"]
    return names[int(((float(yaw) % 360) + 22.5) // 45) % 8]


def _playerdata(world: Path | None, uid: str) -> dict | None:
    if world is None:
        return None
    path = world / "playerdata" / f"{uid}.dat"
    if not path.is_file():
        return None
    try:
        return nbt.load(path)
    except (OSError, nbt.NbtError) as exc:
        logger.info("playerdata %s nicht lesbar: %s", uid, exc)
        return None


def world_weather(world: Path | None) -> str | None:
    """Wetter aus level.dat (Stand des letzten Speicherns)."""
    if world is None or not (world / "level.dat").is_file():
        return None
    try:
        data = nbt.load(world / "level.dat").get("Data") or {}
    except (OSError, nbt.NbtError):
        return None
    if data.get("thundering"):
        return "gewitter"
    if data.get("raining"):
        return "regen"
    return "klar"


# ---------------------------------------------------------------------------
# Liste und Profil
# ---------------------------------------------------------------------------

def _known(instance_id: str) -> tuple:
    """(Spieler je UUID, Welt-Ordner)."""
    base = instances.instance_dir(instance_id)
    world = instances.world_dir(instance_id)
    players: dict = {}

    def entry(uid: str, name: str | None = None) -> dict:
        player = players.setdefault(uid, {"uuid": uid, "name": None, "op_level": 0,
                                          "whitelisted": False, "banned": False})
        if name and not player["name"]:
            player["name"] = str(name)[:32]
        return player

    for item in _entries(base / "usercache.json"):
        uid = _uuid(item.get("uuid"))
        if uid:
            entry(uid, item.get("name"))
    for item in _entries(base / "ops.json"):
        uid = _uuid(item.get("uuid"))
        if uid:
            entry(uid, item.get("name"))["op_level"] = int(item.get("level") or 4)
    for item in _entries(base / "whitelist.json"):
        uid = _uuid(item.get("uuid"))
        if uid:
            entry(uid, item.get("name"))["whitelisted"] = True
    for item in _entries(base / "banned-players.json"):
        uid = _uuid(item.get("uuid"))
        if uid:
            entry(uid, item.get("name"))["banned"] = True
    for sub, suffix in (("playerdata", ".dat"), ("stats", ".json"),
                        ("advancements", ".json")):
        for uid in _uuid_files(world / sub if world else None, suffix):
            entry(uid)
        if len(players) > _MAX_PLAYERS:
            break
    return players, world


def list_players(instance_id: str, online: list | None = None) -> dict:
    """Alle bekannten Spieler mit Kurzinfos; online = Namen laut RCON 'list'."""
    instance = instances.get_instance(instance_id)
    players, world = _known(instance_id)
    online_set = {n.lower() for n in (online or [])}
    playtime = {p["player"]: p for p in
                history.query_playtime("all", instance_id).get("players", [])}
    out = []
    names_seen = set()
    for uid, player in list(players.items())[:_MAX_PLAYERS]:
        stats = summarize_stats(_read_json(world / "stats" / f"{uid}.json")) if world else {}
        adv = summarize_advancements(
            _read_json(world / "advancements" / f"{uid}.json")) if world else []
        name = player["name"]
        tracked = playtime.get(name or "") or {}
        if name:
            names_seen.add(name.lower())
        out.append({
            **player,
            "online": bool(name and name.lower() in online_set),
            "play_seconds": stats.get("play_seconds") or tracked.get("seconds") or 0,
            "last_seen": tracked.get("last_seen"),
            "advancements": len(adv),
            "player_kills": stats.get("player_kills", 0),
            "deaths": stats.get("deaths", 0),
        })
    # Online, aber (noch) ohne Datei — z. B. gerade zum ersten Mal verbunden
    for name in online or []:
        if name.lower() not in names_seen:
            out.append({"uuid": None, "name": name, "op_level": 0, "whitelisted": False,
                        "banned": False, "online": True, "play_seconds": 0,
                        "last_seen": None, "advancements": 0, "player_kills": 0,
                        "deaths": 0})
    out.sort(key=lambda p: (not p["online"], -(p["last_seen"] or 0),
                            (p["name"] or "~").lower()))
    return {"players": out, "world": world.name if world else None,
            "game_version": instance.get("game_version")}


def _find(instance_id: str, key: str) -> tuple:
    players, world = _known(instance_id)
    uid = _uuid(key)
    if uid and uid in players:
        return players[uid], world
    lowered = key.lower()
    for player in players.values():
        if (player["name"] or "").lower() == lowered:
            return player, world
    if inventory_mod.PLAYER_RE.match(key):
        return {"uuid": None, "name": key, "op_level": 0, "whitelisted": False,
                "banned": False}, world
    raise HTTPException(status_code=404, detail="Spieler nicht gefunden")


def player_profile(instance_id: str, key: str) -> dict:
    """Profil eines Spielers: Rollen, gespeicherte Werte, Statistiken,
    Rekorde, Fortschritte, Sitzungen."""
    instance = instances.get_instance(instance_id)
    player, world = _find(instance_id, key)
    uid = player.get("uuid")
    raw_stats = _read_json(world / "stats" / f"{uid}.json") if world and uid else None
    stats = summarize_stats(raw_stats)
    adv = summarize_advancements(
        _read_json(world / "advancements" / f"{uid}.json")) if world and uid else []
    titles, total = advancement_names(instance.get("game_version") or "")
    for item in adv:
        item["title"] = titles.get(item["id"])
    saved = _playerdata(world, uid) if uid else None
    saved_at = None
    if saved is not None and world is not None:
        try:
            saved_at = int((world / "playerdata" / f"{uid}.dat").stat().st_mtime)
        except OSError:
            saved_at = None
    sessions = history.player_sessions(instance_id, player["name"] or "") \
        if player.get("name") else history.player_sessions(instance_id, "")
    return {
        **player,
        "saved": vitals(saved) if saved is not None else None,
        "saved_at": saved_at,
        "stats": stats,
        "advancements": adv,
        "advancements_total": total,
        "sessions": sessions,
        "fetched_at": int(time.time()),
    }


# ---------------------------------------------------------------------------
# Live (RCON)
# ---------------------------------------------------------------------------

def live_commands(names: list) -> list:
    return [f"data get entity {name} {field}" for name in names for field in LIVE_FIELDS]


def parse_live(names: list, outputs: list) -> list:
    """RCON-Antworten (Reihenfolge wie live_commands) → Werte je Spieler."""
    rows = []
    width = len(LIVE_FIELDS)
    for index, name in enumerate(names):
        chunk = outputs[index * width:(index + 1) * width]
        data: dict = {}
        offline = False
        for field, output in zip(LIVE_FIELDS, chunk, strict=False):
            try:
                value = inventory_mod.parse_data_get(output)
            except inventory_mod.PlayerOffline:
                offline = True
                break
            if value is not None:
                data[field] = inventory_mod.to_plain(value)
        if offline:
            continue
        rows.append({"name": name, **vitals(data)})
    return rows
