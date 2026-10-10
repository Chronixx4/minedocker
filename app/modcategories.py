"""Mod-Kategorien für den Modbrowser-Filter („Was kann die Mod?“).

Die Listen kommen von den Anbietern selbst (Modrinth /tag/category,
CurseForge /categories) und werden einen Tag gecacht; die Namen werden
für bekannte Kategorien ins Deutsche übersetzt, unbekannte behalten den
Anbieternamen. Fällt Modrinth aus, gibt es eine eingebaute Liste.
"""
import re
import time

from fastapi import HTTPException

from . import curseforge, modrinth
from .config import settings
from .searchcache import shared_client

_TTL = 86400.0
_CACHE: dict = {}

# Modrinth-Kategorie-Namen sind zugleich die Facet-Werte (categories:<name>)
MODRINTH_RE = re.compile(r"^[a-z0-9-]{1,40}$")
CF_ID_RE = re.compile(r"^\d{1,8}$")

_MODRINTH_DE = {
    "adventure": "Abenteuer",
    "cursed": "Kurioses",
    "decoration": "Dekoration",
    "economy": "Wirtschaft",
    "equipment": "Ausrüstung & Waffen",
    "food": "Essen",
    "game-mechanics": "Spielmechanik",
    "library": "Bibliothek (für andere Mods)",
    "magic": "Magie",
    "management": "Verwaltung",
    "minigame": "Minispiele",
    "mobs": "Kreaturen",
    "optimization": "Optimierung",
    "social": "Soziales",
    "storage": "Lagerung",
    "technology": "Technik",
    "transportation": "Transport",
    "utility": "Nützliches",
    "worldgen": "Weltgenerierung",
}

# CurseForge-Slugs (Unterkategorien von „Mods“)
_CF_DE = {
    "world-gen": "Weltgenerierung",
    "biomes": "Biome",
    "ores-resources": "Erze & Rohstoffe",
    "structures": "Strukturen",
    "dimensions": "Dimensionen",
    "mc-mobs": "Kreaturen",
    "mobs": "Kreaturen",
    "technology": "Technik",
    "technology-processing": "Verarbeitung",
    "technology-player-transport": "Spieler-Transport",
    "technology-item-fluid-energy-transport": "Energie-, Flüssigkeits- & Item-Transport",
    "technology-farming": "Landwirtschaft",
    "technology-energy": "Energie",
    "technology-automation": "Automatisierung",
    "technology-genetics": "Genetik",
    "redstone": "Redstone",
    "magic": "Magie",
    "storage": "Lagerung",
    "library-api": "Bibliothek & API",
    "adventure-rpg": "Abenteuer & RPG",
    "map-information": "Karte & Informationen",
    "cosmetic": "Kosmetik",
    "mc-addons": "Addons",
    "mc-miscellaneous": "Sonstiges",
    "armor-weapons-tools": "Rüstung, Waffen & Werkzeuge",
    "server-utility": "Server-Werkzeuge",
    "mc-food": "Essen",
    "food": "Essen",
    "utility-qol": "Nützliches & Komfort",
    "education": "Bildung",
    "performance": "Optimierung",
    "bug-fixes": "Fehlerbehebungen",
    "mc-creator": "MCreator",
    "twitch-integration": "Twitch-Integration",
}


def _cached(key: str):
    entry = _CACHE.get(key)
    if entry and time.monotonic() - entry[0] < _TTL:
        return entry[1]
    return None


def _fallback_modrinth() -> list:
    return sorted(({"id": k, "name": v} for k, v in _MODRINTH_DE.items()),
                  key=lambda c: c["name"].lower())


async def modrinth_categories() -> list:
    cached = _cached("modrinth")
    if cached is not None:
        return cached
    try:
        client = shared_client("modrinth-meta", timeout=15.0)
        resp = await client.get(f"{settings.modrinth_api}/tag/category",
                                headers=modrinth._headers())
        data = resp.json() if resp.status_code == 200 else None
    except Exception:
        data = None
    if not isinstance(data, list):
        return _fallback_modrinth()  # nicht cachen → später erneut versuchen
    out = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        # Nur Mod-Kategorien (Header „categories“); Ressourcenpaket-/Shader-
        # Merkmale wie Auflösung oder „performance impact“ gehören nicht hierher
        if entry.get("project_type") != "mod" or entry.get("header") != "categories":
            continue
        if not MODRINTH_RE.match(name):
            continue
        out.append({"id": name, "name": _MODRINTH_DE.get(name, name.replace("-", " ").title())})
    if not out:
        return _fallback_modrinth()
    out.sort(key=lambda c: c["name"].lower())
    _CACHE["modrinth"] = (time.monotonic(), out)
    return out


async def curseforge_categories() -> list:
    if not settings.cf_api_key:
        return []
    cached = _cached("curseforge")
    if cached is not None:
        return cached
    try:
        client = shared_client("curseforge-meta", factory=curseforge._new_client)
        data = await curseforge._get_json(client, "/categories", params={
            "gameId": curseforge.GAME_ID, "classId": curseforge.CLASS_MOD})
    except HTTPException:
        return []
    entries = (data.get("data") or []) if isinstance(data, dict) else []
    by_id = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("isClass"):
            continue
        cid = entry.get("id")
        if cid is None or not CF_ID_RE.match(str(cid)):
            continue
        slug = str(entry.get("slug") or "")
        by_id[str(cid)] = {
            "id": str(cid),
            "name": _CF_DE.get(slug) or str(entry.get("name") or slug),
            "parent": str(entry.get("parentCategoryId") or ""),
        }
    out = []
    for cat in by_id.values():
        parent = by_id.get(cat["parent"])
        # Unterkategorien (z. B. Technik: Energie) mit Oberbegriff anzeigen
        name = f"{parent['name']}: {cat['name']}" if parent else cat["name"]
        out.append({"id": cat["id"], "name": name})
    out.sort(key=lambda c: c["name"].lower())
    if out:
        _CACHE["curseforge"] = (time.monotonic(), out)
    return out


async def categories(source: str) -> list:
    if source == "curseforge":
        return await curseforge_categories()
    return await modrinth_categories()


def clear() -> None:
    _CACHE.clear()
