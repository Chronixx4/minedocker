"""Geplante Aufgaben je Instanz (Scheduler).

Features (je Instanz konfigurierbar über instance.json → 'schedule'):
- Auto-Start: Instanzen mit schedule.auto_start werden beim Dashboard-Start
  automatisch gestartet (deckt Host-Reboots ab, weil Compose das Dashboard
  wiederbelebt). Ein manuell gestoppter Server bleibt gestoppt — der Auto-
  Start greift nur einmal pro Dashboard-Start.
- Geplanter Neustart: täglich zur Uhrzeit schedule.restart.time ('HH:MM',
  Container-Lokalzeit) mit RCON-Vorwarnungen ('say') warn_minutes davor
  (0 = keine Vorwarnung). Verpasste Termine (Dashboard war z. B. unten)
  werden nur innerhalb eines 30-Minuten-Fensters nachgeholt, sonst
  übersprungen und für den nächsten Tag markiert.
- Geplanter Stopp: täglich zur Uhrzeit schedule.stop.time die laufende
  Instanz stoppen (gleiche Vorwarn-/Nachhol-Logik wie der Neustart) —
  z. B. nachts automatisch herunterfahren.
- Zeitgesteuerte Backups: alle schedule.backup.interval_hours ein
  'scheduled-*'-Snapshot (eigene Rotation, max. keep).
- Geplanter Mod-Update-Check: alle schedule.update_check.interval_hours
  prüfen; bei Updates einen Alert senden (Ereignis 'update' — opt-in über
  ALERT_EVENTS, z. B. ALERT_EVENTS=crash,update).

Der Zustand (letzte Ausführungen je Instanz) liegt in
scheduler_state.json neben INSTANCES_DIR (/data/scheduler_state.json) und
überlebt Dashboard-Neustarts; Einträge gelöschter Instanzen räumt der Tick
weg. Die Schleife läuft als Daemon-Thread aus dem App-Lifespan, wirft nie
und beendet sich beim Shutdown.
"""
import asyncio
import datetime as dt
import json
import logging
import threading
from pathlib import Path

from . import backups, instances, rcon, runtime, updates, watchdog
from .config import settings

logger = logging.getLogger("dashboard.scheduler")

TICK_SECONDS = 30.0          # Dauer zwischen zwei Durchläufen
_AUTO_START_RETRIES = 5      # Boot-Versuche, falls Docker noch nicht bereit ist
_GRACE_MINUTES = 30          # Nachlauf-Fenster für verpasste Termin-Aktionen

STOP = threading.Event()
_LOCK = threading.Lock()     # schützt Zustandsdatei + Tick gegen Überlappung

_boot_attempts = 0
_auto_start_done = False


def state_path() -> Path:
    """Pfad der Zustandsdatei (Geschwister von INSTANCES_DIR, im Volume)."""
    return Path(settings.instances_dir).parent / "scheduler_state.json"


def load_state() -> dict:
    """Zustand laden; defekt/fehlt → leer (Scheduler startet neu gezählt)."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    """Zustand atomar schreiben; Fehler nur loggen (Scheduler läuft weiter)."""
    path = state_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("Scheduler-Zustand nicht schreibbar: %s", exc)


def reset_for_tests() -> None:
    """Boot-Merken zurücksetzen (nur Tests; State-Datei bleibt unberührt)."""
    global _boot_attempts, _auto_start_done
    _boot_attempts = 0
    _auto_start_done = False
    STOP.clear()


def _now() -> dt.datetime:
    """Tick-Uhr (monkeypatchbar in Tests)."""
    return dt.datetime.now()


# ---------------------------------------------------------------------------
# Auto-Start (einmal pro Dashboard-Start)
# ---------------------------------------------------------------------------

def run_auto_start() -> int:
    """Startet alle Instanzen mit auto_start, die nicht laufen.
    Rückgabe: Anzahl tatsächlich gestarteter Instanzen. Wirft nie."""
    started = 0
    for instance in instances.list_instances():
        if not (instance.get("schedule") or {}).get("auto_start"):
            continue
        name = instance.get("name") or instance["id"]
        try:
            if runtime.is_running(instance):
                instances.set_status(instance["id"], "running", None)
                continue
            instances.set_status(instance["id"], "starting", None)
            runtime.start_instance(instance)
            instances.set_status(instance["id"], "running", None)
            started += 1
            logger.info("Auto-Start: %s", name)
        except Exception as exc:
            logger.warning("Auto-Start fehlgeschlagen: %s — %s", name, exc)
            try:  # Status auf den tatsächlichen Container-Zustand zurücksetzen
                instances.set_status(
                    instance["id"],
                    "running" if runtime.is_running(instance) else "stopped", None)
            except Exception:
                pass
    return started


# ---------------------------------------------------------------------------
# Geplanter Neustart (mit RCON-Vorwarnung)
# ---------------------------------------------------------------------------

def _rcon_say(instance: dict, message: str) -> bool:
    """RCON 'say' an die laufende Instanz (best effort, wirft nie)."""
    try:
        host, port = runtime.rcon_target(instance)
        rcon.command(host, port, runtime.rcon_secret(instance), f"say {message}")
        return True
    except Exception as exc:
        logger.warning("Vorwarnung nicht sendbar (%s): %s",
                       instance.get("name"), exc)
        return False


def _scheduled_restart(instance: dict) -> None:
    """Führt den geplanten Neustart aus (gleicher Ablauf wie die Restart-Route):
    erwarteter Stop (Watchdog meldet keinen Crash), Stop, Start, Status."""
    instance_id = instance["id"]
    instances.set_status(instance_id, "starting", None)
    watchdog.expect_stop(instance_id)
    try:
        runtime.stop_instance(instance)
        runtime.start_instance(instance)
    except RuntimeError as exc:
        detail = f"Geplanter Neustart fehlgeschlagen: {exc}"
        instances.set_status(instance_id, "error", detail)
        watchdog.notify("crash",
                        f"[mc-dashboard] Geplanter Neustart fehlgeschlagen: "
                        f"{instance.get('name')} — {exc}")
        raise RuntimeError(detail) from exc
    instances.set_status(instance_id, "running", None)


def _handle_daily(instance: dict, entry: dict, now: dt.datetime, section: str,
                  label: str, fire, warn_fmt) -> list:
    """Gemeinsame Termin-Logik für geplanten Neustart/Stopp (Abschnitt
    'restart'/'stop'): HH:MM in Container-Lokalzeit, RCON-Vorwarnungen
    ('say', Stufen warn_minutes und 1 Min vorher, nur bei laufendem Server),
    Feuertag markieren bevor die Aktion läuft (Fehler wiederholen nicht im
    30-s-Takt), verpasste Termine nur im Nachhol-Fenster (_GRACE_MINUTES)
    nachgeholt. entry wird mutiert (Keys '{section}_last'/'{section}_warns').
    Rückgabe: Liste erfolgter Aktionen ('warn' und/oder section).
    fire(instance) führt die Aktion aus; wirft nicht nach außen."""
    cfg = (instance.get("schedule") or {}).get(section) or {}
    if not cfg.get("enabled"):
        return []
    raw = str(cfg.get("time") or instances.schedule_defaults(section).get("time"))
    try:
        hh, mm = (int(part) for part in raw.split(":", 1))
    except ValueError:
        return []  # ungültige Zeit → stillschweigend überspringen
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return []
    warn_min = cfg.get("warn_minutes")
    try:
        warn_min = max(0, min(30, int(warn_min if warn_min is not None else 5)))
    except (TypeError, ValueError):
        warn_min = 5

    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    fired_today = entry.get(f"{section}_last") == today
    actions: list = []

    # Vorwarnungen: vor dem Termin, wenn heute noch nicht gefeuert wurde
    if not fired_today and now < target:
        stages = sorted({m for m in (warn_min, 1) if m > 0}) if warn_min > 0 else []
        sent = set(entry.get(f"{section}_warns") or [])
        for stage in stages:
            if stage in sent:
                continue
            if now >= target - dt.timedelta(minutes=stage):
                if not runtime.is_running(instance):
                    continue  # keine Warnung ohne laufenden Server
                if _rcon_say(instance, warn_fmt(stage, raw)):
                    entry.setdefault(f"{section}_warns", []).append(stage)
                    actions.append("warn")

    # Aktion am Termin (inkl. Nachhol-Fenster); vor der Ausführung
    # markieren, damit ein Fehler nicht im 30-s-Takt wiederholt wird
    if now >= target and not fired_today:
        entry[f"{section}_last"] = today
        entry[f"{section}_warns"] = []
        actions.append(section)
        if now - target <= dt.timedelta(minutes=_GRACE_MINUTES):
            try:
                if runtime.is_running(instance):
                    logger.info("Geplanter %s: %s (%s Uhr)", label,
                                instance.get("name"), raw)
                    fire(instance)
                else:
                    logger.info("Geplanter %s übersprungen (Instanz läuft "
                                "nicht): %s", label, instance.get("name"))
            except Exception:
                logger.exception("Geplanter %s fehlgeschlagen: %s", label,
                                 instance.get("name"))
        else:
            logger.info("Geplanter %s verpasst (> %d Min nach Termin): %s",
                        label, _GRACE_MINUTES, instance.get("name"))
    return actions


def handle_restart(instance: dict, entry: dict, now: dt.datetime) -> list:
    """Fällige Aktionen des geplanten Neustarts für eine Instanz.

    entry ist der Zustands-Eintrag (wird mutiert): 'restart_last' = Datum des
    letzten Feuertags, 'restart_warns' = gesendete Vorwarnstufen (Minuten).
    Rückgabe: Liste erfolgter Aktionen ('warn' und/oder 'restart')."""
    return _handle_daily(
        instance, entry, now, "restart", "Neustart", _scheduled_restart,
        lambda stage, raw: f"Server-Neustart in {stage} Minute(n) "
                           f"(geplant {raw} Uhr)")


# ---------------------------------------------------------------------------
# Geplanter Stopp (täglich, mit RCON-Vorwarnung)
# ---------------------------------------------------------------------------

def _scheduled_stop(instance: dict) -> None:
    """Stoppt die Instanz (erwarteter Stop — der Watchdog meldet keinen
    Crash). Fehler: Status 'error' + Crash-Alert, wie beim Neustart."""
    instance_id = instance["id"]
    try:
        runtime.stop_instance(instance)
    except RuntimeError as exc:
        detail = f"Geplanter Stopp fehlgeschlagen: {exc}"
        instances.set_status(instance_id, "error", detail)
        watchdog.notify("crash",
                        f"[mc-dashboard] Geplanter Stopp fehlgeschlagen: "
                        f"{instance.get('name')} — {exc}")
        raise RuntimeError(detail) from exc
    instances.set_status(instance_id, "stopped", None)


def _fire_scheduled_stop(instance: dict) -> None:
    """Stopp-Aktion für _handle_daily: Watchdog-Stop vorher melden."""
    watchdog.expect_stop(instance["id"])
    _scheduled_stop(instance)


def handle_stop(instance: dict, entry: dict, now: dt.datetime) -> list:
    """Fällige Aktionen des geplanten Stopps für eine Instanz (gleiche
    Termin-/Nachhol-Logik wie handle_restart). entry-Keys: 'stop_last' =
    Datum des letzten Feuertags, 'stop_warns' = gesendete Vorwarnstufen.
    Rückgabe: Liste erfolgter Aktionen ('warn' und/oder 'stop')."""
    return _handle_daily(
        instance, entry, now, "stop", "Stopp", _fire_scheduled_stop,
        lambda stage, raw: f"Server stoppt in {stage} Minute(n) "
                           f"(geplant {raw} Uhr)")


# ---------------------------------------------------------------------------
# Zeitgesteuerte Backups
# ---------------------------------------------------------------------------

def _schedule_int(section: str, key: str, cfg: dict) -> int:
    """Zahl aus einem Zeitplan-Abschnitt mit Bereichs-/Default-Fallback."""
    _lo, _hi, default = instances._SCHEDULE_RANGES[(section, key)]
    raw = cfg.get(key)
    if raw is None:
        return default
    try:
        return max(_lo, min(_hi, int(raw)))
    except (TypeError, ValueError):
        return default


def handle_backup(instance: dict, entry: dict, now: dt.datetime) -> bool:
    """Fälliges zeitgesteuertes Backup ausführen. Rückgabe: True bei Backup.
    Erstes enabled-Tick setzt nur die Fälligkeit (kein Sofort-Backup)."""
    cfg = (instance.get("schedule") or {}).get("backup") or {}
    if not cfg.get("enabled"):
        return False
    interval_seconds = _schedule_int("backup", "interval_hours", cfg) * 3600
    keep = _schedule_int("backup", "keep", cfg)
    now_ts = now.timestamp()
    last = entry.get("backup_last")
    if last is None:
        entry["backup_last"] = now_ts
        return False
    if now_ts - float(last) < interval_seconds:
        return False
    backups.scheduled_backup(instance["id"], instances.instance_dir(instance["id"]),
                             keep)
    entry["backup_last"] = now_ts
    return True


# ---------------------------------------------------------------------------
# Geplanter Mod-Update-Check (Alert, opt-in über ALERT_EVENTS 'update')
# ---------------------------------------------------------------------------

def handle_update_check(instance: dict, entry: dict, now: dt.datetime) -> bool:
    """Fälligen Update-Check ausführen; bei Updates Alert senden.
    Rückgabe: True wenn geprüft wurde. Erstes enabled-Tick setzt Fälligkeit."""
    cfg = (instance.get("schedule") or {}).get("update_check") or {}
    if not cfg.get("enabled"):
        return False
    interval_seconds = _schedule_int("update_check", "interval_hours", cfg) * 3600
    now_ts = now.timestamp()
    last = entry.get("update_last")
    if last is None:
        entry["update_last"] = now_ts
        return False
    if now_ts - float(last) < interval_seconds:
        return False
    try:
        result = asyncio.run(updates.check_updates(instance["id"]))
    except Exception as exc:
        logger.warning("Geplanter Update-Check fehlgeschlagen (%s): %s",
                       instance.get("name"), exc)
        entry["update_last"] = now_ts  # trotzdem zählen — nächster Versuch later
        return False
    entry["update_last"] = now_ts
    try:
        updatable = int(result.get("updatable") or 0)
    except (TypeError, ValueError):
        updatable = 0
    if updatable > 0:
        names = ", ".join(
            str(item.get("filename") or "?")
            for item in (result.get("items") or [])
            if item.get("status") == updates.S_UPDATE)[:400]
        watchdog.notify(
            "update",
            f"[mc-dashboard] {updatable} Mod-Update(s) verfügbar: "
            f"{instance.get('name')} — {names}")
    return True


# ---------------------------------------------------------------------------
# Tick + Loop
# ---------------------------------------------------------------------------

def tick() -> dict:
    """Ein Scheduler-Durchlauf: alle Instanzen prüfen, fällige Aktionen
    ausführen, Zustand wegschreiben, verwaiste Einträge entfernen.
    Wirft nicht pro Instanz; Rückgabe: Zähler je Aktionstyp."""
    counts = {"warn": 0, "restart": 0, "stop": 0, "backup": 0, "update_check": 0}
    now = _now()
    with _LOCK:
        state = load_state()
        live_ids: set[str] = set()
        for instance in instances.list_instances():
            instance_id = str(instance.get("id") or "")
            live_ids.add(instance_id)
            if not (instance.get("schedule") or {}):
                continue
            entries = state.setdefault("instances", {})
            if not isinstance(entries, dict):
                state["instances"] = entries = {}
            entry = entries.setdefault(instance_id, {})
            if not isinstance(entry, dict):
                entries[instance_id] = entry = {}
            try:
                for action in handle_restart(instance, entry, now):
                    if action in counts:
                        counts[action] += 1
                for action in handle_stop(instance, entry, now):
                    if action in counts:
                        counts[action] += 1
                if handle_backup(instance, entry, now):
                    counts["backup"] += 1
                if handle_update_check(instance, entry, now):
                    counts["update_check"] += 1
            except Exception:
                logger.exception("Scheduler-Aktion fehlgeschlagen: %s (%s)",
                                 instance.get("name"), instance_id)
        entries = state.get("instances")
        if isinstance(entries, dict):
            for stale in [k for k in entries if k not in live_ids]:
                entries.pop(stale, None)
        save_state(state)
    return counts


def scheduler_loop(stop: threading.Event) -> None:
    """Daemon-Thread aus dem Lifespan: Auto-Start beim Boot, danach regelmäßige
    Ticks. Beendet sich mit dem Shutdown-Signal; wirft nie nach außen."""
    global _boot_attempts, _auto_start_done
    logger.info("Scheduler aktiv (Tick alle %.0f s)", TICK_SECONDS)
    while not stop.is_set():
        if not _auto_start_done:
            try:
                run_auto_start()
                _auto_start_done = True
            except Exception:
                _boot_attempts += 1
                logger.exception("Auto-Start fehlgeschlagen (Versuch %d/%d)",
                                 _boot_attempts, _AUTO_START_RETRIES)
                if _boot_attempts >= _AUTO_START_RETRIES:
                    logger.warning("Auto-Start nach %d Versuchen aufgegeben",
                                   _boot_attempts)
                    _auto_start_done = True
        try:
            tick()
        except Exception:
            logger.exception("Scheduler-Tick-Fehler")
        if stop.wait(TICK_SECONDS):
            return
    logger.info("Scheduler gestoppt")
