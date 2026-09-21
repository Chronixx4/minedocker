"""Katalog: verfügbare Minecraft-Spielversionen und Loader-Versionen.

Quellen:
- Minecraft-Versionen: Mojang piston-meta (offiziell, Releases + Snapshots)
- Fabric:      https://meta.fabricmc.net/v2/versions/loader/{mc}
- Quilt:       https://meta.quiltmc.org/v3/versions/loader/{mc}
- Forge:       promotions_slim.json (latest/recommended je Version)
- NeoForge:    Maven-Metadaten (Versionen sind an die MC-Version gekoppelt)
- Paper:       PaperMC-API (Build wird vom Server-Image automatisch gewählt)
- Bukkit/Spigot: kein automatischer Download verfügbar (BuildTools) — Hinweis
"""
import asyncio
import time

import httpx
from fastapi import HTTPException

from .config import ALLOWED_LOADERS, settings

_CACHE: dict = {}
_CACHE_TTL = 600.0
_HTTP_TIMEOUT = 15.0

# Loader, für die wir Meta-Versionen anbieten können (Bukkit/Spigot ohne Auto-Download)
_META_LOADERS = ("fabric", "forge", "neoforge", "quilt", "paper")


def _headers() -> dict:
    return {"User-Agent": settings.user_agent, "Accept": "application/json"}


def _cached(key: str):
    entry = _CACHE.get(key)
    if entry and time.time() - entry[1] < _CACHE_TTL:
        return entry[0]
    return None


def _store(key: str, value) -> None:
    _CACHE[key] = (value, time.time())


async def _get_json(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=_headers())
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502,
                            detail=f"Katalog-Server nicht erreichbar ({exc.__class__.__name__})")
    if resp.status_code != 200:
        raise HTTPException(status_code=502,
                            detail=f"Katalog-Server antwortete mit HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Katalog-Server lieferte ungültige Daten") from exc


async def mc_versions() -> dict:
    """Alle verfügbaren Minecraft-Versionen (Releases + Snapshots, neueste zuerst)."""
    cached = _cached("mc_versions")
    if cached is not None:
        return cached
    data = await _get_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
    versions = data.get("versions") or []
    releases = [v["id"] for v in versions if v.get("type") == "release" and v.get("id")]
    snapshots = [v["id"] for v in versions if v.get("type") == "snapshot" and v.get("id")]
    result = {
        "releases": releases,
        "snapshots": snapshots,
        "latest_release": (data.get("latest") or {}).get("release") or (releases[0] if releases else None),
    }
    _store("mc_versions", result)
    return result


def _fabric_versions(payload) -> list:
    versions = []
    for entry in payload or []:
        loader = (entry or {}).get("loader") or {}
        version = loader.get("version")
        if version:
            versions.append({"version": str(version), "stable": bool(loader.get("stable"))})
    return versions


def _quilt_versions(payload) -> list:
    versions = []
    for entry in payload or []:
        loader = (entry or {}).get("loader") or {}
        version = loader.get("version")
        if version:
            versions.append({"version": str(version), "stable": not entry.get("beta")})
    return versions


def _forge_versions(payload, game_version: str) -> list:
    promotions = payload.get("promos") or {}
    latest = promotions.get(f"{game_version}-latest")
    recommended = promotions.get(f"{game_version}-recommended")
    if not latest and not recommended:
        return []
    found, seen = [], set()
    for value in (recommended, latest):
        if value and value not in seen:
            seen.add(value)
            found.append({"version": str(value), "stable": value == recommended})
    return found


def _neoforge_prefix(game_version: str) -> str:
    # "1.21.1" → "21.1", "1.21" → "21" (NeoForge-Versionen folgen diesem Schema)
    parts = game_version.split(".")
    if len(parts) >= 2 and parts[0] == "1":
        return ".".join(parts[1:])
    return game_version


def _neoforge_versions(payload, game_version: str) -> list:
    prefix = _neoforge_prefix(game_version)
    versions = []
    for value in payload.get("versions") or []:
        if str(value).startswith(f"{prefix}.") or str(value) == prefix:
            versions.append({"version": str(value), "stable": True})
    return versions


async def _loader_for(loader: str, game_version: str) -> dict:
    """Holt die Loader-Versionen eines Loaders; nie werfend (Fehler = leer)."""
    try:
        if loader == "fabric":
            payload = await _get_json(f"https://meta.fabricmc.net/v2/versions/loader/{game_version}")
            versions = _fabric_versions(payload)
            note = None
        elif loader == "quilt":
            payload = await _get_json(f"https://meta.quiltmc.org/v3/versions/loader/{game_version}")
            versions = _quilt_versions(payload)
            note = None
        elif loader == "forge":
            payload = await _get_json(
                "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json")
            versions = _forge_versions(payload, game_version)
            note = None if versions else "Für diese MC-Version sind keine Forge-Promotion-Versionen gelistet"
        elif loader == "neoforge":
            payload = await _get_json(
                "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge")
            versions = _neoforge_versions(payload, game_version)
            note = None if versions else "Keine NeoForge-Version für diese MC-Version gefunden"
        elif loader == "paper":
            payload = await _get_json("https://fill.papermc.io/v3/projects/paper")
            groups = payload.get("versions") or {}
            available: list[str] = []
            if isinstance(groups, dict):
                # v3: {"1.21": ["1.21.11", ...], "1.20": [...]} (neueste zuerst)
                for group_versions in groups.values():
                    if isinstance(group_versions, list):
                        available.extend(str(v) for v in group_versions)
            elif isinstance(groups, list):
                available = [str(v) for v in groups]
            if game_version in available:
                versions = [{"version": "auto", "stable": True}]
                note = None
            else:
                versions = []
                note = "Paper unterstützt diese MC-Version (noch) nicht"
        else:  # bukkit / spigot
            versions = []
            note = "Bukkit/Spigot erfordert BuildTools — Loader-Version leer lassen (Standard wird genutzt)"
        return {"loader": loader, "versions": versions, "note": note, "error": None}
    except HTTPException as exc:
        return {"loader": loader, "versions": [], "note": None, "error": str(exc.detail)}
    except Exception as exc:  # Katalog darf nie das UI blockieren
        return {"loader": loader, "versions": [], "note": None,
                "error": f"{exc.__class__.__name__}: {exc}"}


async def loaders(game_version: str) -> dict:
    """Verfügbare Loader + deren Versionen für eine Minecraft-Version."""
    from .security import validate_identifier
    validate_identifier(game_version, "Minecraft-Version")
    cache_key = f"loaders:{game_version}"
    cached = _cached(cache_key)
    if cached is not None:
        return cached
    results = await asyncio.gather(*(_loader_for(loader, game_version)
                                     for loader in _META_LOADERS))
    plain = [dict(entry) for entry in results]
    for loader in ALLOWED_LOADERS:
        if loader not in _META_LOADERS:
            plain.append(await _loader_for(loader, game_version))
    result = {"game_version": game_version, "loaders": plain}
    _store(cache_key, result)
    return result


__all__ = ["loaders", "mc_versions"]
