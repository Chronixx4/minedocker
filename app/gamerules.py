"""Gamerule-Quick-Editor: kuratierte Liste der Vanilla-1.21.x-Gamerules
(Java Edition) mit Typ, Default und sinnvollem Wertebereich.

- GET: RCON 'gamerule' ohne Argumente listet alle gesetzten Regeln
  ('gamerule <name> = <value>'); nur bei laufender Instanz (409 sonst).
- POST: RCON 'gamerule <name> <value>' — Name gegen die kuratierte Liste
  geprüft (400 unbekannt), Wert typisiert validiert (bool true/false, int
  im Bereich) — das verhindert Injektion über den RCON-String.
- Regeln, die der Server nicht meldet (z. B. auf älteren 1.21-Patchständen
  fehlende), liefern value=None; das Frontend zeigt dann den Default.
- Ab 1.21.11 (Snapshot 25w44a) heißen die Regeln anders (snake_case, z. B.
  doDaylightCycle → advance_time, disableRaids → raids mit umgekehrter
  Bedeutung). Die API bleibt bei den alten Namen; übersetzt wird hier beim
  Lesen und Setzen (NEW_NAMES, INVERTED). Quelle: Befehlsbaum der Versionen
  1.21.10 und 1.21.11 (misode/mcmeta).
- 'pvp' ist bewusst NICHT enthalten: Bedrock-only, in Java Edition gibt es
  keine pvp-Gamerule. showCoordinates ebenfalls weggelassen (Bedrock-only).
"""
import logging
import re

from fastapi import HTTPException

logger = logging.getLogger("dashboard.gamerules")

_INT_MAX = 2**31 - 1


def _g(name: str, gtype: str, default, desc: str = "",
       lo: int | None = None, hi: int | None = None,
       since: tuple | None = None) -> dict:
    entry = {"name": name, "type": gtype, "default": default, "desc": desc}
    if since:
        entry["since"] = since
    if gtype == "int":
        entry["min"] = lo
        entry["max"] = hi
    return entry


GAMERULES: list = [
    _g("announceAdvancements", "bool", True,
       "Fortschritts-Meldungen im Chat anzeigen"),
    _g("blockExplosionDropDecay", "bool", True,
       "Bei Block-Explosionen gehen manche Blöcke verloren"),
    _g("commandBlockOutput", "bool", True,
       "Befehlsblock-Ausgaben im Chat/Log"),
    _g("commandModificationBlockLimit", "int", 32768,
       "Limit für von Befehlen geänderte Blöcke", lo=0, hi=_INT_MAX),
    _g("commandModificationEntityLimit", "int", 256,
       "Limit für von Befehlen geänderte Entitäten", lo=0, hi=_INT_MAX),
    _g("disableElytraMovementCheck", "bool", False,
       "Server-Prüfung für Elytra-Geschwindigkeit aus"),
    _g("disableRaids", "bool", False, "Räuber-Überfälle deaktivieren"),
    _g("doDaylightCycle", "bool", True, "Tag-/Nacht-Zyklus"),
    _g("doEntityDrops", "bool", True, "Entitäten hinterlegen Items (Boots, Loren…)"),
    _g("doFireTick", "bool", True, "Feuer breitet sich aus und erlischt"),
    _g("fireSpreadRadius", "int", 128,
       "Radius um Spieler, in dem sich Feuer ausbreitet (0 = aus, -1 = überall; "
       "ab 1.21.11)", lo=-1, hi=_INT_MAX),
    _g("doImmediateRespawn", "bool", False, "Respawn ohne Todes-Bildschirm"),
    _g("doInsomnia", "bool", True, "Phantome spawnen bei Schlafmangel"),
    _g("doLimitedCrafting", "bool", False,
       "Nur freigeschaltete Rezepte craftbar"),
    _g("doMobSpawning", "bool", True, "Natürliches Mobs-Spawning"),
    _g("doPatrolSpawning", "bool", True, "Räuber-Patrouillen spawnen"),
    _g("doTraderSpawning", "bool", True, "Wandernde Händler spawnen"),
    _g("doVinesSpread", "bool", True, "Ranken wachsen"),
    _g("doWardenSpawning", "bool", True, "Warden spawnen aus Sculk"),
    _g("doWeatherCycle", "bool", True, "Wetter wechselt"),
    _g("enderPearlsVanishOnDeath", "bool", True,
       "Enderperlen verschwinden beim Tod des Werfers (ab 1.21.2)"),
    _g("fallDamage", "bool", True, "Fallschaden"),
    _g("fireDamage", "bool", True, "Feuerschaden"),
    _g("forgiveDeadPlayers", "bool", True, "Ärgerliche Zombies werden friedlich"),
    _g("freezeDamage", "bool", True, "Schaden durch Einfrieren (Pulverschnee)"),
    _g("globalSoundEvents", "bool", True,
       "Weltweite Geräusche (z. B. Enderdrachen-Tod)"),
    _g("keepInventory", "bool", False, "Inventar beim Tod behalten"),
    _g("lavaSourceConversion", "bool", True,
       "Fließende Lava wird zur Quelle (mit Spitze+Kessel)"),
    _g("locatorBar", "bool", True,
       "Ortungsleiste zeigt, wo andere Spieler sind (ab 1.21.6)",
       since=(1, 21, 6)),
    _g("logAdminCommands", "bool", True, "Admin-Befehle im Server-Log"),
    _g("maxCommandChainLength", "int", 65535,
       "Max. Länge einer Befehlsblock-Kette", lo=0, hi=_INT_MAX),
    _g("maxCommandForkCount", "int", 65536,
       "Max. Forks bei execute-Befehlen", lo=0, hi=_INT_MAX),
    _g("maxEntityCramming", "int", 24,
       "Entitäten pro Block, bevor Schadensdruck entsteht", lo=0, hi=_INT_MAX),
    _g("minecartMaxSpeed", "int", 8,
       "Max. Loren-Geschwindigkeit (Blöcke/s, ab 1.21.2)", lo=1, hi=1024),
    _g("mobExplosionDropDecay", "bool", True,
       "Bei Mob-Explosionen gehen manche Drops verloren"),
    _g("mobGriefing", "bool", True,
       "Mobs verändern Blöcke (Creeper, Endermen, Schafe…)"),
    _g("naturalRegeneration", "bool", True, "Natürliche HP-Regeneration"),
    _g("playersSleepingPercentage", "int", 100,
       "Anteil schlafender Spieler (%), um die Nacht zu überspringen",
       lo=0, hi=100),
    _g("playersNetherPortalCreativeDelay", "int", 0,
       "Netherportal-Verzögerung Kreativ (Ticks)", lo=0, hi=_INT_MAX),
    _g("playersNetherPortalDefaultDelay", "int", 80,
       "Netherportal-Verzögerung Standard (Ticks)", lo=0, hi=_INT_MAX),
    _g("randomTickSpeed", "int", 3,
       "Zufalls-Ticks pro Chunk-Sektion (0 = aus)", lo=0, hi=_INT_MAX),
    _g("reducedDebugInfo", "bool", False,
       "Debug-Info (F3) für Spieler einschränken"),
    _g("respawnBlocksExplode", "bool", True,
       "Respawn-Anker/Betten explodieren in falscher Dimension (ab 1.21.2)"),
    _g("sendCommandFeedback", "bool", True,
       "Befehls-Rückmeldungen an Spieler"),
    _g("showDeathMessages", "bool", True, "Todesnachrichten im Chat"),
    _g("snowAccumulationHeight", "int", 1,
       "Max. Schneeschicht-Höhe", lo=0, hi=16),
    _g("spectatorGenerateEvents", "bool", True,
       "Zuschauer erzeugen Beobachter-Events"),
    _g("spawnRadius", "int", 10,
       "Respawn-Streuung um den Spawn-Punkt", lo=0, hi=65536),
    _g("tntExplosionDropDecay", "bool", True,
       "Bei TNT-Explosionen gehen manche Drops verloren"),
    _g("universalAnger", "bool", False,
       "Ärgerliche Mobs zielen auf alle Spieler"),
    _g("waterSourceConversion", "bool", True,
       "Fließendes Wasser wird zur Quelle (mit 2 Nachbarquellen)"),
]

_BY_NAME = {g["name"]: g for g in GAMERULES}

# Namen ab 1.21.11. Fehlt eine Regel hier, gibt es sie dort nicht mehr.
NEW_NAMES = {
    "announceAdvancements": "show_advancement_messages",
    "blockExplosionDropDecay": "block_explosion_drop_decay",
    "commandBlockOutput": "command_block_output",
    "commandModificationBlockLimit": "max_block_modifications",
    "disableElytraMovementCheck": "elytra_movement_check",
    "disableRaids": "raids",
    "doDaylightCycle": "advance_time",
    "doEntityDrops": "entity_drops",
    "doImmediateRespawn": "immediate_respawn",
    "doInsomnia": "spawn_phantoms",
    "doLimitedCrafting": "limited_crafting",
    "doMobSpawning": "spawn_mobs",
    "doPatrolSpawning": "spawn_patrols",
    "doTraderSpawning": "spawn_wandering_traders",
    "doVinesSpread": "spread_vines",
    "doWardenSpawning": "spawn_wardens",
    "doWeatherCycle": "advance_weather",
    "enderPearlsVanishOnDeath": "ender_pearls_vanish_on_death",
    "fallDamage": "fall_damage",
    "fireDamage": "fire_damage",
    "fireSpreadRadius": "fire_spread_radius_around_player",
    "forgiveDeadPlayers": "forgive_dead_players",
    "freezeDamage": "freeze_damage",
    "globalSoundEvents": "global_sound_events",
    "keepInventory": "keep_inventory",
    "lavaSourceConversion": "lava_source_conversion",
    "locatorBar": "locator_bar",
    "logAdminCommands": "log_admin_commands",
    "maxCommandChainLength": "max_command_sequence_length",
    "maxCommandForkCount": "max_command_forks",
    "maxEntityCramming": "max_entity_cramming",
    "minecartMaxSpeed": "max_minecart_speed",
    "mobExplosionDropDecay": "mob_explosion_drop_decay",
    "mobGriefing": "mob_griefing",
    "naturalRegeneration": "natural_health_regeneration",
    "playersNetherPortalCreativeDelay": "players_nether_portal_creative_delay",
    "playersNetherPortalDefaultDelay": "players_nether_portal_default_delay",
    "playersSleepingPercentage": "players_sleeping_percentage",
    "randomTickSpeed": "random_tick_speed",
    "reducedDebugInfo": "reduced_debug_info",
    "sendCommandFeedback": "send_command_feedback",
    "showDeathMessages": "show_death_messages",
    "snowAccumulationHeight": "max_snow_accumulation_height",
    "spawnRadius": "respawn_radius",
    "tntExplosionDropDecay": "tnt_explosion_drop_decay",
    "universalAnger": "universal_anger",
    "waterSourceConversion": "water_source_conversion",
}
# Neue Regel bedeutet das Gegenteil der alten (disableRaids=true ⇔ raids=false).
INVERTED = frozenset({"disableElytraMovementCheck", "disableRaids"})
# Nur in neuen Versionen vorhanden
_NEW_ONLY = frozenset({"fireSpreadRadius"})


def known(name: str) -> dict | None:
    return _BY_NAME.get((name or "").strip())


def _version_tuple(version: str) -> tuple:
    nums = re.findall(r"\d+", (version or "").split("-")[0])
    return tuple(int(n) for n in nums[:3]) or (0,)


def new_names(game_version: str) -> bool:
    """Gamerule-Namen in snake_case: ab 1.21.11 bzw. Snapshot 25w44a."""
    snap = re.match(r"^(\d{2})w(\d{2})", game_version or "")
    if snap:
        return (int(snap.group(1)), int(snap.group(2))) >= (25, 44)
    return _version_tuple(game_version) >= (1, 21, 11)


def available(entry: dict, game_version: str) -> bool:
    """Gibt es die Regel in dieser MC-Version? Snapshots gelten als neu."""
    if new_names(game_version):
        return entry["name"] in NEW_NAMES
    if entry["name"] in _NEW_ONLY:
        return False
    since = entry.get("since")
    if not since or re.match(r"^\d{2}w\d{2}", game_version or ""):
        return True
    return _version_tuple(game_version) >= since


def rcon_name(entry: dict, game_version: str) -> str:
    """Name, unter dem der Server die Regel kennt."""
    if new_names(game_version):
        return NEW_NAMES[entry["name"]]
    return entry["name"]


def _invert(entry: dict, game_version: str) -> bool:
    return new_names(game_version) and entry["name"] in INVERTED


def parse_query_value(output: str) -> str | None:
    """'Gamerule <name> is currently set to: <value>' → value."""
    match = re.search(r"(?i)is currently set to:\s*(\S+)", output or "")
    return match.group(1).strip() if match else None


# Uhrzeit-/Wetter-Knöpfe der Welt-Einstellungen: feste Befehle, kein
# freier Text vom Browser.
TIME_PRESETS = {"morgen": "time set day", "mittag": "time set noon",
                "abend": "time set 12000", "nacht": "time set midnight"}
WEATHER_PRESETS = {"klar": "weather clear", "regen": "weather rain",
                   "gewitter": "weather thunder"}


def validate_value(entry: dict, value) -> str:
    """Wert gegen Typ/Bereich prüfen; Rückgabe: kanonischer RCON-String."""
    if entry["type"] == "bool":
        if isinstance(value, bool):
            return "true" if value else "false"
        text = str(value).strip().lower()
        if text in ("true", "false"):
            return text
        raise HTTPException(status_code=400,
                            detail=f"'{entry['name']}' ist eine bool-Gamerule — "
                                   "erlaubt: true/false")
    if entry["type"] == "int":
        try:
            number = int(value) if not isinstance(value, bool) else \
                (1 if value else 0)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"'{entry['name']}' ist eine int-Gamerule — ganzzahliger "
                       "Wert erwartet") from None
        lo, hi = entry.get("min"), entry.get("max")
        if (lo is not None and number < lo) or (hi is not None and number > hi):
            raise HTTPException(
                status_code=400,
                detail=f"'{entry['name']}' muss zwischen {lo} und {hi} liegen")
        return str(number)
    raise HTTPException(status_code=400, detail="Unbekannter Gamerule-Typ")


def parse_gamerules_output(output: str) -> dict:
    """RCON-Ausgabe parsen. Erkannt werden:
    - Listen-Format (/gamerule ohne Argumente): 'gamerule <name> = <value>'
    - Abfrage-Format (/gamerule <name>): 'Gamerule <name> is currently set to: <value>'
    (Groß-/Kleinschreibung tolerant; nur Namen der kuratierten Liste.)"""
    values: dict = {}
    if not output:
        return values
    for match in re.finditer(
            r"(?im)^\s*gamerule\s+([A-Za-z0-9_]+)\s*=\s*(\S+)\s*$", output):
        values[match.group(1)] = match.group(2).strip()
    for match in re.finditer(
            r"(?i)gamerule\s+([A-Za-z0-9_]+)\s+is currently set to:\s*(\S+)",
            output):
        values[match.group(1)] = match.group(2).strip()
    return {name: value for name, value in values.items() if known(name)}


def _typed_value(entry: dict, raw: str | None):
    """RCON-String → bool/int (None bei Unparsable)."""
    if raw is None:
        return None
    if entry["type"] == "bool":
        text = str(raw).strip().lower()
        return text == "true" if text in ("true", "false") else None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _rcon(instance: dict, command: str) -> str:
    from . import rcon as rcon_mod  # lazy, vermeidet Import-Zirkel
    from . import runtime
    host, port = runtime.rcon_target(instance)
    try:
        return rcon_mod.command(host, port, runtime.rcon_secret(instance),
                                command, timeout=15.0)
    except (TimeoutError, rcon_mod.RconError, OSError) as exc:
        raise HTTPException(status_code=503,
                            detail=f"RCON nicht erreichbar: {exc}") from exc


def _rcon_many(instance: dict, cmds: list) -> list:
    from . import rcon as rcon_mod  # lazy, vermeidet Import-Zirkel
    from . import runtime
    host, port = runtime.rcon_target(instance)
    try:
        return rcon_mod.commands(host, port, runtime.rcon_secret(instance),
                                 cmds, timeout=15.0)
    except (TimeoutError, rcon_mod.RconError, OSError) as exc:
        raise HTTPException(status_code=503,
                            detail=f"RCON nicht erreichbar: {exc}") from exc


def parse_daytime(output: str) -> int | None:
    """'The time is 6000' → 6000 (Ticks seit Tagesbeginn, 0 bis 23999)."""
    match = re.search(r"(-?\d+)", output or "")
    return int(match.group(1)) % 24000 if match else None


def read_gamerules(instance_id: str) -> dict:
    """Alle kuratierten Gamerules mit aktuellem Wert (RCON, nur laufend)."""
    from . import instances  # lazy, vermeidet Import-Zirkel
    instance = instances.get_instance(instance_id)
    from . import runtime
    if not runtime.is_running(instance):
        raise HTTPException(status_code=409,
                            detail="Gamerules sind nur bei laufender Instanz "
                                   "lesbar — Server starten")
    version = instance.get("game_version") or ""
    rules_here = [e for e in GAMERULES if available(e, version)]
    # Vanilla kennt kein 'gamerule' ohne Argumente — jede Regel einzeln
    # abfragen, alles über eine RCON-Verbindung.
    cmds = [f"gamerule {rcon_name(e, version)}" for e in rules_here]
    outputs = _rcon_many(instance, [*cmds, "time query daytime"])
    rules = []
    for entry, output in zip(rules_here, outputs, strict=False):
        raw = parse_query_value(output)
        value = _typed_value(entry, raw)
        if value is not None and _invert(entry, version):
            value = not value
        rules.append({
            "name": entry["name"],
            "type": entry["type"],
            "value": value,
            "default": entry["default"],
            "min": entry.get("min"),
            "max": entry.get("max"),
            "desc": entry.get("desc") or "",
            "raw": raw,
        })
    return {"gamerules": rules, "daytime": parse_daytime(outputs[-1])}


def set_gamerule(instance_id: str, name: str, value) -> dict:
    """Eine Gamerule setzen (RCON); Antwort = neuer Stand."""
    from . import instances  # lazy, vermeidet Import-Zirkel
    instance = instances.get_instance(instance_id)
    from . import runtime
    entry = known(name)
    if entry is None:
        raise HTTPException(status_code=400,
                            detail=f"Unbekannte Gamerule '{name}' — nur die "
                                   "kuratierte Vanilla-1.21.x-Liste ist erlaubt")
    if not available(entry, instance.get("game_version") or ""):
        raise HTTPException(status_code=400,
                            detail=f"'{entry['name']}' gibt es in dieser "
                                   "Minecraft-Version nicht")
    if not runtime.is_running(instance):
        raise HTTPException(status_code=409,
                            detail="Gamerules sind nur bei laufender Instanz "
                                   "setzbar — Server starten")
    canonical = validate_value(entry, value)
    version = instance.get("game_version") or ""
    sent = canonical
    if _invert(entry, version):
        sent = "false" if canonical == "true" else "true"
    output = _rcon(instance, f"gamerule {rcon_name(entry, version)} {sent}")
    typed = _typed_value(entry, canonical)
    logger.info("Gamerule gesetzt: %s = %s (%s)", entry["name"], canonical,
                instance_id)
    return {"name": entry["name"], "value": typed, "output": output}


def set_time_weather(instance_id: str, time: str | None,
                     weather: str | None) -> dict:
    """Uhrzeit und/oder Wetter über feste Voreinstellungen setzen (RCON)."""
    from . import instances, runtime  # lazy, vermeidet Import-Zirkel
    instance = instances.get_instance(instance_id)
    cmds = []
    if time is not None:
        if time not in TIME_PRESETS:
            raise HTTPException(status_code=400, detail="Unbekannte Uhrzeit")
        cmds.append(TIME_PRESETS[time])
    if weather is not None:
        if weather not in WEATHER_PRESETS:
            raise HTTPException(status_code=400, detail="Unbekanntes Wetter")
        cmds.append(WEATHER_PRESETS[weather])
    if not cmds:
        raise HTTPException(status_code=400,
                            detail="Uhrzeit oder Wetter angeben")
    if not runtime.is_running(instance):
        raise HTTPException(status_code=409,
                            detail="Nur bei laufender Instanz möglich — "
                                   "Server starten")
    outputs = _rcon_many(instance, [*cmds, "time query daytime"])
    logger.info("Welt: %s (%s)", ", ".join(cmds), instance_id)
    return {"commands": cmds, "daytime": parse_daytime(outputs[-1])}


__all__ = [
    "GAMERULES",
    "known",
    "parse_gamerules_output",
    "read_gamerules",
    "set_gamerule",
    "set_time_weather",
    "validate_value",
]
