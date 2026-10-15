"""TPS/MSPT-Abfrage per RCON (loaderabhängiges Kommando) und Lag-Erkennung.

- vanilla/Fabric/Quilt: 'tick query' (ab Minecraft 1.20.3) → mittlere Tickzeit
- Paper/Spigot/Bukkit/Purpur: 'tps'
- Forge: 'forge tps', NeoForge: 'neoforge tps'
Ältere Server ohne passendes Kommando liefern None (keine Anzeige).
"""
import re

from . import rcon as rcon_mod
from . import runtime

_COLOR_RE = re.compile(r"(?:§.|\x1b\[[0-9;]*m)")
_NUM = r"(\d+(?:[.,]\d+)?)"
_MSPT_RE = re.compile(r"(?:average time per tick|mean tick time)\D*" + _NUM, re.I)
_MEAN_TPS_RE = re.compile(r"mean tps\D*" + _NUM, re.I)
_PAPER_RE = re.compile(r"tps from last[^:]*:\s*\*?" + _NUM, re.I)

LAG_TPS = 15.0       # darunter gilt der Server als laggend
LAG_TICKS = 4        # so viele Scheduler-Ticks (je 30 s) in Folge → Alert


def command_for(loader: str | None) -> str:
    loader = (loader or "").lower()
    if loader in ("paper", "spigot", "bukkit", "purpur", "folia"):
        return "tps"
    if loader == "neoforge":
        return "neoforge tps"
    if loader == "forge":
        return "forge tps"
    return "tick query"


def _num(text: str) -> float:
    return float(text.replace(",", "."))


def parse(output: str) -> dict | None:
    """{'tps': float, 'mspt': float|None} oder None, wenn nichts erkennbar."""
    text = _COLOR_RE.sub("", output or "")
    mspt = None
    match = _MSPT_RE.search(text)
    if match:
        mspt = _num(match.group(1))
    match = _MEAN_TPS_RE.search(text) or _PAPER_RE.search(text)
    if match:
        tps = min(20.0, _num(match.group(1)))
    elif mspt is not None and mspt > 0:
        tps = min(20.0, 1000.0 / mspt)
    else:
        return None
    return {"tps": round(tps, 1), "mspt": None if mspt is None else round(mspt, 1)}


def query(instance: dict) -> dict | None:
    """Blockierend; None bei nicht unterstütztem Kommando. Wirft RconError."""
    host, port = runtime.rcon_target(instance)
    out = rcon_mod.command(host, port, runtime.rcon_secret(instance),
                           command_for(instance.get("loader")))
    return parse(out)


def is_lagging(sample: dict | None) -> bool:
    return sample is not None and sample["tps"] < LAG_TPS
