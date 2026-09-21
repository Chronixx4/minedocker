"""Crash-Watchdog: Docker-Container-Events beobachten, Instanz-Status setzen
und optionale Alerts (Discord-/Telegram-Webhook) versenden.

- Beobachtet die Container-Events 'die' und 'start' aller verwalteten
  Instanz-Container (mc-inst-*) über die Docker-Events-API.
- 'die' mit Exit-Code != 0 (und nicht als erwarteter Stop gemeldet) setzt den
  Instanz-Status auf 'error' (Crash) und kann einen Webhook auslösen.
- 'die' mit Exit-Code 0 oder als erwarteter Stop gemeldet (Dashboard-Stop/
  Neustart/Löschen) setzt 'stopped' und kann einen Stop-Alert auslösen.
- 'start' setzt 'running' (deckt auch Docker-Restarts der Restart-Policy ab)
  und kann einen Start-Alert auslösen.

Alerts (optional, konfigurierbar über Env):
- ALERT_WEBHOOK_URL: Discord-kompatibler Webhook (POST {"content": ...})
- TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID: Telegram sendMessage
- ALERT_EVENTS: Komma-separiert crash,start,stop (Default: alle drei)

Die Schleife (watchdog_loop) läuft als Daemon-Thread aus dem App-Lifespan,
reconnectet bei Docker-Fehlern endlos und beendet sich beim Shutdown.
"""
import logging
import threading
import time

from .config import settings

logger = logging.getLogger("dashboard.watchdog")

_NAME_PREFIX = "mc-inst-"
_STOP_GRACE = 120.0  # Sekunden, in denen ein gemeldeter Stop als solcher gilt
_RETRY_SECONDS = 15.0

# Erwartete (intentionale) Container-Stops: {instanz_id: deadline}
_expected_stops: dict = {}
_LOCK = threading.Lock()

# Shutdown-Signal für die Event-Schleife (vom Lifespan gesetzt)
STOP = threading.Event()


def expect_stop(instance_id: str) -> None:
    """Markiert einen bevorstehenden, intentionalen Container-Stopp, damit der
    Watchdog das 'die'-Event nicht als Crash wertet (auch bei Exit-Code != 0)."""
    with _LOCK:
        _expected_stops[instance_id] = time.time() + _STOP_GRACE


def _pop_expected_stop(instance_id: str) -> bool:
    """Nimmt eine noch gültige Stop-Markierung auf (Einmalverwendung)."""
    with _LOCK:
        deadline = _expected_stops.pop(instance_id, None)
    return bool(deadline and time.time() <= deadline)


def _alert_events() -> set:
    return {e.strip().lower() for e in settings.alert_events.split(",")
            if e.strip()}


def _post_json(url: str, payload: dict) -> None:
    import httpx  # lazy: nur benötigt, wenn ein Webhook konfiguriert ist
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(url, json=payload)
        if resp.status_code >= 300:
            raise RuntimeError(f"HTTP {resp.status_code}")


def notify(event: str, message: str) -> None:
    """Versendet den Alert an alle konfigurierten Ziele; wirft nie —
    ein Webhook-Fehler darf den Watchdog nicht blockieren."""
    if event not in _alert_events():
        return
    targets = []
    if settings.alert_webhook_url:
        discord_url = settings.alert_webhook_url
        targets.append((discord_url, {"content": message[:1900]}))
    if settings.telegram_bot_token and settings.telegram_chat_id:
        tg_url = (f"https://api.telegram.org/bot"
                  f"{settings.telegram_bot_token}/sendMessage")
        targets.append((tg_url, {"chat_id": settings.telegram_chat_id,
                                 "text": message[:3500]}))
    for url, payload in targets:
        try:
            _post_json(url, payload)
        except Exception as exc:
            logger.warning("Alert-Webhook fehlgeschlagen (%s…): %s",
                           url[:48], exc)


def handle_event(event: dict) -> str | None:
    """Verarbeitet ein Docker-Event und setzt den Instanz-Status.
    Rückgabe: 'crash' | 'stop' | 'start' | None (ignoriert)."""
    if str(event.get("Type") or "") != "container":
        return None
    action = str(event.get("Action") or "")
    if action not in ("die", "start"):
        return None
    attrs = ((event.get("Actor") or {}).get("Attributes")) or {}
    name = str(attrs.get("name") or "")
    if not name.startswith(_NAME_PREFIX):
        return None  # fremder Container
    instance_id = name[len(_NAME_PREFIX):]
    from . import instances  # lazy, vermeidet Import-Zirkel
    try:
        instance = instances.get_instance(instance_id)
    except Exception:
        return None  # Instanz (inzwischen) gelöscht
    if action == "start":
        instances.set_status(instance_id, "running", None)
        logger.info("Watchdog: Container gestartet: %s", instance_id)
        notify("start", f"[mc-dashboard] Server gestartet: {instance['name']}")
        return "start"
    # 'die': Exit-Code entscheidet Crash (≠ 0) vs. sauberes Ende (0)
    try:
        exit_code = int(attrs.get("exitCode") or 0)
    except (TypeError, ValueError):
        exit_code = 0
    if _pop_expected_stop(instance_id) or exit_code == 0:
        instances.set_status(instance_id, "stopped", None)
        logger.info("Watchdog: Container gestoppt: %s (Exit %s)",
                    instance_id, exit_code)
        notify("stop", f"[mc-dashboard] Server gestoppt: {instance['name']}")
        return "stop"
    detail = f"Container unerwartet beendet (Exit-Code {exit_code})"
    instances.set_status(instance_id, "error", detail)
    logger.warning("Watchdog: Container-Crash erkannt: %s (%s)",
                   instance_id, detail)
    notify("crash",
           f"[mc-dashboard] Server-CRASH: {instance['name']} — {detail}")
    return "crash"


def _event_filter() -> dict:
    return {"type": "container", "event": ["die", "start"]}


def watchdog_loop(stop: threading.Event,
                  poll_interval: float = _RETRY_SECONDS) -> None:
    """Blockierende Event-Schleife (Daemon-Thread aus dem Lifespan): liest den
    Docker-Events-Stream, reconnectet bei Fehlern endlos und beendet sich mit
    dem Shutdown-Signal. Ohne Docker-Verbindung wartet sie (einmal geloggt)."""
    from . import runtime  # lazy, vermeidet Import-Zirkel
    warned_offline = False
    while not stop.is_set():
        try:
            client = runtime._get_client()
        except RuntimeError as exc:
            if not warned_offline:
                logger.warning("Watchdog ohne Docker-Daemon: %s", exc)
                warned_offline = True
            if stop.wait(poll_interval):
                return
            continue
        try:
            for event in client.events(decode=True, filters=_event_filter()):
                if stop.is_set():
                    return
                try:
                    handle_event(event)
                except Exception:
                    logger.exception("Watchdog-Event-Fehler")
        except Exception as exc:
            logger.warning("Watchdog-Event-Stream endete (%s), reconnect…",
                           exc.__class__.__name__)
        if stop.wait(poll_interval):
            return
