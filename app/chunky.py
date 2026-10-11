"""Chunky-Tab: Pregeneration per RCON steuern (Mod oder Plugin 'Chunky').

- Erkennung: aktive Chunky-.jar in mods/ (Fabric/Forge/NeoForge) oder
  plugins/ (Paper/Spigot).
- Einstellungen (Welt, Mittelpunkt, Radius, Form, Muster) werden validiert
  und als feste 'chunky …'-Kommandos gebaut — kein freier Text zum Server.
- Fortschritt: Ausgabe von 'chunky progress' wird geparst (Prozent, Chunks,
  ETA, Rate); Farbcodes (§x, ANSI) werden vorher entfernt.
"""
import re

from fastapi import HTTPException

SHAPES = ("square", "circle", "triangle", "diamond", "pentagon", "hexagon", "star")
PATTERNS = ("concentric", "loop", "spiral", "region", "world")
ACTIONS = ("start", "pause", "continue", "cancel", "progress", "apply", "worldborder")

_WORLD_RE = re.compile(r"^[A-Za-z0-9_.:/\- ]{1,64}$")
_COORD_MAX = 30_000_000
_RADIUS_MAX = 30_000_000
_COLOR_RE = re.compile(r"(?:§.|\x1b\[[0-9;]*m)")


def find_chunky(instance_id: str) -> dict:
    """Aktive Chunky-.jar suchen: {'installed': bool, 'jar': name|None,
    'kind': 'mod'|'plugin'|None, 'disabled': bool}."""
    from . import instances  # lazy, vermeidet Import-Zirkel
    root = instances.instance_dir(instance_id)
    disabled = False
    for folder, kind in (("mods", "mod"), ("plugins", "plugin")):
        try:
            paths = sorted((root / folder).glob("*.jar*"))
        except OSError:
            continue
        for path in paths:
            name = path.name.lower()
            if "chunky" not in name:
                continue
            if name.endswith(".jar"):
                return {"installed": True, "jar": path.name, "kind": kind,
                        "disabled": False}
            if name.endswith(".jar.disabled"):
                disabled = True
    return {"installed": False, "jar": None, "kind": None, "disabled": disabled}


def _int(value, label: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{label} muss eine ganze Zahl sein")
    if not low <= number <= high:
        raise HTTPException(status_code=400,
                            detail=f"{label} muss zwischen {low} und {high} liegen")
    return number


def build_commands(req: dict) -> list:
    """Kommandoliste aus einer Anfrage (Einstellungen + Aktion) bauen.

    'apply' setzt nur die Einstellungen, 'start' setzt sie und startet,
    'worldborder' übernimmt Mittelpunkt/Radius der Weltgrenze; pause/continue/
    cancel/progress sind einzelne Kommandos."""
    action = req.get("action")
    if action not in ACTIONS:
        raise HTTPException(status_code=400, detail="Unbekannte Chunky-Aktion")
    cmds = []
    if action in ("start", "apply", "worldborder"):
        world = (req.get("world") or "").strip()
        if world:
            if not _WORLD_RE.fullmatch(world):
                raise HTTPException(status_code=400, detail="Ungültiger Weltname")
            cmds.append(f"chunky world {world}")
        if action == "worldborder":
            cmds.append("chunky worldborder")
        else:
            cx, cz = req.get("center_x"), req.get("center_z")
            if cx not in (None, "") or cz not in (None, ""):
                if cx in (None, "") or cz in (None, ""):
                    raise HTTPException(status_code=400,
                                        detail="Mittelpunkt braucht X und Z")
                cmds.append("chunky center "
                            f"{_int(cx, 'X', -_COORD_MAX, _COORD_MAX)} "
                            f"{_int(cz, 'Z', -_COORD_MAX, _COORD_MAX)}")
            radius = req.get("radius")
            if radius not in (None, ""):
                cmds.append(f"chunky radius {_int(radius, 'Radius', 1, _RADIUS_MAX)}")
        shape = req.get("shape")
        if shape:
            if shape not in SHAPES:
                raise HTTPException(status_code=400, detail="Unbekannte Form")
            cmds.append(f"chunky shape {shape}")
        pattern = req.get("pattern")
        if pattern:
            if pattern not in PATTERNS:
                raise HTTPException(status_code=400, detail="Unbekanntes Muster")
            cmds.append(f"chunky pattern {pattern}")
    if action == "start":
        cmds.append("chunky start")
    elif action in ("pause", "continue", "cancel", "progress"):
        cmds.append(f"chunky {action}")
    elif not cmds:
        raise HTTPException(status_code=400, detail="Keine Einstellungen angegeben")
    return cmds


def clean(text: str) -> str:
    return _COLOR_RE.sub("", text or "").strip()


def _num(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def parse_progress(output: str) -> dict:
    """'chunky progress' → {'state', 'world', 'chunks', 'percent', 'eta',
    'rate', 'current', 'text'}; state = running | paused | idle | unknown."""
    text = clean(output)
    low = text.lower()
    result = {"state": "unknown", "world": None, "chunks": None, "percent": None,
              "eta": None, "rate": None, "current": None, "text": text}
    if "no tasks" in low or "no task" in low:
        result["state"] = "idle"
        return result
    world = re.search(r"(?:running|paused) for (\S+?)\.(?:\s|$)", text, re.I)
    if world:
        result["world"] = world.group(1)
    match = re.search(r"Processed:\s*([\d,]+)\s*chunks?\s*\(([\d.,]+)%\)", text, re.I)
    if match:
        result["chunks"] = int(match.group(1).replace(",", ""))
        result["percent"] = _num(match.group(2))
    match = re.search(r"ETA:\s*([\d:]+)", text, re.I)
    if match:
        result["eta"] = match.group(1)
    match = re.search(r"Rate:\s*([\d.,]+)\s*cps", text, re.I)
    if match:
        result["rate"] = _num(match.group(1))
    match = re.search(r"Current:\s*(-?\d+),\s*(-?\d+)", text, re.I)
    if match:
        result["current"] = [int(match.group(1)), int(match.group(2))]
    if "paused" in low:
        result["state"] = "paused"
    elif result["percent"] is not None or "running" in low:
        result["state"] = "running"
    return result


__all__ = ["ACTIONS", "PATTERNS", "SHAPES", "build_commands", "clean",
           "find_chunky", "parse_progress"]
