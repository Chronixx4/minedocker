"""Gemeinsame Bausteine für die Mod-Suche (Modrinth & CurseForge):

- shared_client(): wiederverwendeter HTTP-Client mit Keep-Alive. Jede Suche
  baute bisher einen neuen Client → DNS + TCP + TLS-Handshake pro Seite
  (je nach Netz mehrere hundert Millisekunden, bevor die eigentliche Anfrage
  überhaupt losgeht). Mit Keep-Alive kostet „Weiter“ nur noch die Anfrage.
- TTL-Cache für Suchergebnisse: Zurückblättern und die vom Frontend
  vorab geladene nächste Seite kommen ohne erneute Anbieter-Anfrage.
"""
import asyncio
import copy
import time

import httpx

_SEARCH_TTL = 300.0     # Sekunden, die eine Ergebnisseite gültig bleibt
_SEARCH_MAX = 200       # Obergrenze gecachter Seiten
_KEEPALIVE = 30.0       # Leerlauf-Verbindungen so lange offen halten

_SEARCH_CACHE: dict = {}
_CLIENTS: dict = {}


def shared_client(name: str, factory=None, **kwargs) -> httpx.AsyncClient:
    """Pro Event-Loop wiederverwendeter Client (Keep-Alive).

    factory: Client-Fabrik (Standard httpx.AsyncClient). Wird die Fabrik
    ausgetauscht (Tests mit MockTransport) oder läuft ein neuer Event-Loop,
    entsteht ein frischer Client."""
    loop = asyncio.get_running_loop()
    marker = (factory, httpx.AsyncClient)
    entry = _CLIENTS.get(name)
    if entry and entry[0] is loop and entry[1] == marker and not entry[2].is_closed:
        return entry[2]
    kwargs.setdefault("limits", httpx.Limits(max_keepalive_connections=10,
                                             keepalive_expiry=_KEEPALIVE))
    client = (factory or httpx.AsyncClient)(**kwargs)
    _CLIENTS[name] = (loop, marker, client)
    return client


def cached(key: tuple):
    """Kopie eines gecachten Suchergebnisses oder None (Aufrufer verändern
    die Treffer, z. B. die Installiert-Markierung)."""
    entry = _SEARCH_CACHE.get(key)
    if entry and time.monotonic() - entry[0] < _SEARCH_TTL:
        return copy.deepcopy(entry[1])
    return None


def store(key: tuple, value) -> None:
    if len(_SEARCH_CACHE) >= _SEARCH_MAX:
        now = time.monotonic()
        for k in [k for k, (ts, _v) in _SEARCH_CACHE.items() if now - ts >= _SEARCH_TTL]:
            _SEARCH_CACHE.pop(k, None)
        if len(_SEARCH_CACHE) >= _SEARCH_MAX:
            _SEARCH_CACHE.pop(next(iter(_SEARCH_CACHE)))
    _SEARCH_CACHE[key] = (time.monotonic(), copy.deepcopy(value))


def clear() -> None:
    """Caches leeren (Tests)."""
    _SEARCH_CACHE.clear()
    _CLIENTS.clear()
