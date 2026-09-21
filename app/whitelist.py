"""Whitelist-Verwaltung: whitelist.json direkt bearbeiten — auch offline.

Die Vanilla-Whitelist liegt als whitelist.json im Instanz-Ordner
(Format: [{"uuid": "...", "name": "..."}]). Diese Datei kann editiert
werden, ohne dass der Server läuft:

- Online-Mode-Server: UUID wird über die Mojang-API aufgelöst
  (best effort; ohne Treffer bleibt der Eintrag ohne UUID).
- Offline-Mode-Server (online-mode=false): UUID wird deterministisch
  aus dem Namen berechnet (Java nameUUIDFromBytes("OfflinePlayer:"+name)).

Änderungen greifen beim nächsten Serverstart; bei laufender Instanz kann
die Whitelist per RCON 'whitelist reload' neu geladen werden.
"""
import asyncio
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

import httpx
from fastapi import HTTPException

from .instances import instance_dir

_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
_MAX_ENTRIES = 200
_MOJANG_API = os.getenv("MOJANG_API", "https://api.mojang.com")

WHITELIST_FILE = "whitelist.json"


def validate_name(name: str) -> str:
    """Spielername prüfen (Minecraft-Namen: 1-16 Zeichen, A-Z a-z 0-9 _)."""
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"Ungültiger Spielername: '{(name or '')[:24]}' "
                   "(1-16 Zeichen, Buchstaben/Zahlen/Unterstrich)")
    return name


def _whitelist_path(instance_id: str) -> Path:
    return instance_dir(instance_id) / WHITELIST_FILE


def read_whitelist(instance_id: str) -> dict:
    """whitelist.json lesen (tolerant: fehlt/defekt → leere Liste)."""
    path = _whitelist_path(instance_id)
    entries = []
    exists = path.is_file()
    if exists:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"entries": [], "exists": True, "corrupt": True}
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                entry = {"name": name}
                entry_uuid = _dashed_uuid(item.get("uuid"))
                if entry_uuid:
                    entry["uuid"] = entry_uuid
                entries.append(entry)
    return {"entries": entries, "exists": exists, "corrupt": False}


def read_online_mode(instance_id: str) -> bool:
    """online-mode aus server.properties (Default true)."""
    props = instance_dir(instance_id) / "server.properties"
    try:
        for line in props.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("online-mode="):
                return stripped.split("=", 1)[1].strip().lower() != "false"
    except OSError:
        pass
    return True


def _offline_uuid(name: str) -> str:
    """Offline-UUID wie Java UUID.nameUUIDFromBytes('OfflinePlayer:'+name)."""
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30  # Version 3 (MD5-basiert)
    digest[8] = (digest[8] & 0x3F) | 0x80  # IETF-Variante
    return str(uuid.UUID(bytes=bytes(digest)))


def _dashed_uuid(hex_uuid: object) -> str | None:
    """Mojang-ID (32 Hex-Zeichen) in kanonisches UUID-Format überführen."""
    hex_uuid = str(hex_uuid or "").replace("-", "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", hex_uuid):
        return None
    return (f"{hex_uuid[:8]}-{hex_uuid[8:12]}-{hex_uuid[12:16]}"
            f"-{hex_uuid[16:20]}-{hex_uuid[20:]}")


async def _lookup_uuid_online(name: str) -> str | None:
    """UUID über die Mojang-API auflösen (None bei Fehlern/nicht gefunden)."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{_MOJANG_API}/users/profiles/minecraft/{name}",
                                    headers={"Accept": "application/json"})
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None  # 204/404 = unbekannt, 429 = Rate-Limit → best effort
    try:
        return _dashed_uuid(resp.json().get("id"))
    except ValueError:
        return None


async def resolve_uuids(names: list, online_mode: bool) -> list:
    """UUIDs für alle Namen auflösen: Offline-Mode deterministisch,
    Online-Mode per Mojang-API (parallel, best effort).
    Rückgabe: [(name, uuid|None)] — UUIDs im kanonischen Format."""
    if not online_mode:
        return [(name, _offline_uuid(name)) for name in names]
    lookups = await asyncio.gather(*(_lookup_uuid_online(n) for n in names))
    return [(name, _dashed_uuid(u) if u else None)
            for name, u in zip(names, lookups, strict=False)]


async def write_whitelist(instance_id: str, names: list) -> dict:
    """whitelist.json vollständig ersetzen (atomar). Rückgabe mit geschriebenen
    Einträgen und den Namen, deren UUID nicht aufgelöst werden konnte."""
    names = _dedupe(names)
    online_mode = read_online_mode(instance_id)
    entries, unresolved = [], []
    for name, entry_uuid in await resolve_uuids(names, online_mode):
        entry = {"name": name}
        if entry_uuid:
            entry["uuid"] = entry_uuid
        else:
            unresolved.append(name)
        entries.append(entry)
    path = _whitelist_path(instance_id)
    tmp = path.with_name(WHITELIST_FILE + ".tmp")
    try:
        tmp.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=500,
                            detail=f"whitelist.json nicht schreibbar: {exc}")
    return {"entries": entries, "unresolved": unresolved}


def _dedupe(names: list) -> list:
    """Namen validieren und case-insensitive deduplizieren (erster gewinnt)."""
    seen = set()
    result = []
    for name in names:
        name = validate_name(name)
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(name)
    if len(result) > _MAX_ENTRIES:
        raise HTTPException(status_code=400,
                            detail=f"Zu viele Einträge (max. {_MAX_ENTRIES})")
    return result


async def reload_on_server(instance: dict) -> dict:
    """Whitelist auf dem laufenden Server neu laden (RCON 'whitelist reload').
    Läuft die Instanz nicht, ist kein Reload nötig → {'reloaded': None}."""
    from . import rcon as rcon_mod  # lazy, vermeidet Import-Zirkel
    from . import runtime
    if not runtime.is_running(instance):
        return {"reloaded": None, "reload_error": None}
    host, port = runtime.rcon_target(instance)
    try:
        output = await asyncio.wait_for(
            asyncio.to_thread(rcon_mod.command, host, port,
                              runtime.rcon_secret(instance), "whitelist reload"),
            timeout=15.0)
        return {"reloaded": True, "reload_error": None, "output": output}
    except (TimeoutError, rcon_mod.RconError) as exc:
        return {"reloaded": False, "reload_error": str(exc)}


__all__ = [
    "read_online_mode",
    "read_whitelist",
    "reload_on_server",
    "validate_name",
    "write_whitelist",
]
