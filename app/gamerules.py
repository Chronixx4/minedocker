"""Gamerule-Quick-Editor: kuratierte Liste der Vanilla-1.21.x-Gamerules
(Java Edition) mit Typ, Default und sinnvollem Wertebereich.

- GET: RCON 'gamerule' ohne Argumente listet alle gesetzten Regeln
  ('gamerule <name> = <value>'); nur bei laufender Instanz (409 sonst).
- POST: RCON 'gamerule <name> <value>' — Name gegen die kuratierte Liste
  geprüft (400 unbekannt), Wert typisiert validiert (bool true/false, int
  im Bereich) — das verhindert Injektion über den RCON-String.
- Regeln, die der Server nicht meldet (z. B. auf älteren 1.21-Patchständen
  fehlende), liefern value=None; das Frontend zeigt dann den Default.
- 'pvp' ist bewusst NICHT enthalten: Bedrock-only, in Java Edition gibt es
  keine pvp-Gamerule. showCoordinates ebenfalls weggelassen (Bedrock-only).
"""
import logging

from fastapi import HTTPException

logger = logging.getLogger("dashboard.gamerules")

_INT_MAX = 2**31 - 1


def _g(name: str, gtype: str, default, desc: str = "",
       lo: int | None = None, hi: int | None = None) -> dict:
    entry = {"name": name, "type": gtype, "default": default, "desc": desc}
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


def known(name: str) -> dict | None:
    return _BY_NAME.get((name or "").strip())


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
    import re
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


def read_gamerules(instance_id: str) -> dict:
    """Alle kuratierten Gamerules mit aktuellem Wert (RCON, nur laufend)."""
    from . import instances  # lazy, vermeidet Import-Zirkel
    instance = instances.get_instance(instance_id)
    from . import runtime
    if not runtime.is_running(instance):
        raise HTTPException(status_code=409,
                            detail="Gamerules sind nur bei laufender Instanz "
                                   "lesbar — Server starten")
    output = _rcon(instance, "gamerule")
    live = parse_gamerules_output(output)
    rules = []
    for entry in GAMERULES:
        raw = live.get(entry["name"])
        rules.append({
            "name": entry["name"],
            "type": entry["type"],
            "value": _typed_value(entry, raw),
            "default": entry["default"],
            "min": entry.get("min"),
            "max": entry.get("max"),
            "desc": entry.get("desc") or "",
            "raw": raw,
        })
    return {"gamerules": rules}


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
    if not runtime.is_running(instance):
        raise HTTPException(status_code=409,
                            detail="Gamerules sind nur bei laufender Instanz "
                                   "setzbar — Server starten")
    canonical = validate_value(entry, value)
    output = _rcon(instance, f"gamerule {entry['name']} {canonical}")
    typed = _typed_value(entry, canonical)
    logger.info("Gamerule gesetzt: %s = %s (%s)", entry["name"], canonical,
                instance_id)
    return {"name": entry["name"], "value": typed, "output": output}


__all__ = [
    "GAMERULES",
    "known",
    "parse_gamerules_output",
    "read_gamerules",
    "set_gamerule",
    "validate_value",
]
