"""Tests für den Crash-Watchdog: Event-Verarbeitung, erwartete Stops,
Alert-Webhooks (Discord/Telegram) und die Event-Schleife mit Fake-Docker."""
import shutil
import threading
import time

import pytest

from app import instances, watchdog
from app.config import settings


def _inst(name=None):
    return instances.create_instance(
        name or "Watch-Test", "fabric", "1.21.4", accept_eula=True)


def _event(action: str, name: str, exit_code=None) -> dict:
    attrs = {"name": name, "image": "itzg/minecraft-server:latest"}
    if exit_code is not None:
        attrs["exitCode"] = str(exit_code)
    return {"Type": "container", "Action": action,
            "Actor": {"Attributes": attrs}}


@pytest.fixture(autouse=True)
def _ruhe():
    """Gemeinsamer Zustand des Watchdogs pro Test zurücksetzen."""
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    watchdog._expected_stops.clear()
    watchdog.STOP.clear()
    yield
    watchdog._expected_stops.clear()
    watchdog.STOP.clear()


class TestHandleEvent:
    def test_crash_setzt_status_error(self):
        inst = _inst()
        instances.set_status(inst["id"], "running", None)
        result = watchdog.handle_event(_event("die", f"mc-inst-{inst['id']}", 1))
        assert result == "crash"
        meta = instances.get_instance(inst["id"])
        assert meta["status"] == "error"
        assert "Exit-Code 1" in meta["error"]

    def test_exit_null_gilt_als_stop(self):
        inst = _inst()
        instances.set_status(inst["id"], "running", None)
        result = watchdog.handle_event(_event("die", f"mc-inst-{inst['id']}", 0))
        assert result == "stop"
        assert instances.get_instance(inst["id"])["status"] == "stopped"

    def test_erwarteter_stop_ist_kein_crash(self):
        inst = _inst()
        instances.set_status(inst["id"], "running", None)
        watchdog.expect_stop(inst["id"])
        result = watchdog.handle_event(_event("die", f"mc-inst-{inst['id']}", 143))
        assert result == "stop"
        assert instances.get_instance(inst["id"])["status"] == "stopped"

    def test_erwarteter_stop_verfaellt(self):
        inst = _inst()
        instances.set_status(inst["id"], "running", None)
        watchdog.expect_stop(inst["id"])
        watchdog._expected_stops[inst["id"]] = 0.0  # Deadline abgelaufen
        result = watchdog.handle_event(_event("die", f"mc-inst-{inst['id']}", 1))
        assert result == "crash"

    def test_start_setzt_running(self):
        inst = _inst()
        result = watchdog.handle_event(_event("start", f"mc-inst-{inst['id']}"))
        assert result == "start"
        assert instances.get_instance(inst["id"])["status"] == "running"

    def test_fremder_container_ignoriert(self):
        assert watchdog.handle_event(_event("die", "fremd-abc", 1)) is None
        assert watchdog.handle_event(_event("die", "minecraft", 1)) is None

    def test_geloeschte_instanz_ignoriert(self):
        assert watchdog.handle_event(_event("die", "mc-inst-xxxxxxxx", 1)) is None

    def test_andere_ereignisse_ignoriert(self):
        inst = _inst()
        for action in ("destroy", "kill", "oom", "health_status"):
            event = _event(action, f"mc-inst-{inst['id']}", 1)
            assert watchdog.handle_event(event) is None
        assert watchdog.handle_event(
            {"Type": "image", "Action": "pull", "Actor": {"Attributes": {}}}) is None

    def test_ungueltiger_exit_code_gilt_als_stop(self):
        inst = _inst()
        instances.set_status(inst["id"], "running", None)
        event = _event("die", f"mc-inst-{inst['id']}")
        event["Actor"]["Attributes"]["exitCode"] = "kaputt"
        assert watchdog.handle_event(event) == "stop"


class TestAlerts:
    def _ziele(self, monkeypatch, events="crash,start,stop"):
        monkeypatch.setattr(settings, "alert_webhook_url",
                            "https://discord.example/webhook")
        monkeypatch.setattr(settings, "telegram_bot_token", "bot-token")
        monkeypatch.setattr(settings, "telegram_chat_id", "42")
        monkeypatch.setattr(settings, "alert_events", events)
        calls = []

        def fake_post(url, payload):
            calls.append((url, payload))

        monkeypatch.setattr(watchdog, "_post_json", fake_post)
        return calls

    def test_crash_postet_discord_und_telegram(self, monkeypatch):
        calls = self._ziele(monkeypatch)
        watchdog.notify("crash", "Server-CRASH: Test")
        assert len(calls) == 2
        urls = [url for url, _ in calls]
        assert any(url.startswith("https://discord.example/") for url in urls)
        assert any("api.telegram.org/botbot-token/sendMessage" in url
                   for url in urls)
        payloads = [p for _, p in calls]
        assert any("Server-CRASH" in p.get("content", "") for p in payloads)
        assert any(p.get("chat_id") == "42" for p in payloads)

    def test_ereignisfilter(self, monkeypatch):
        calls = self._ziele(monkeypatch, events="crash")
        watchdog.notify("stop", "gestoppt")
        watchdog.notify("start", "gestartet")
        assert calls == []
        watchdog.notify("crash", "CRASH")
        assert len(calls) == 2

    def test_webhook_fehler_wird_verschluckt(self, monkeypatch):
        self._ziele(monkeypatch)

        def kaputt(url, payload):
            raise RuntimeError("netz weg")

        monkeypatch.setattr(watchdog, "_post_json", kaputt)
        watchdog.notify("crash", "CRASH")  # darf nicht werfen

    def test_ohne_ziele_passiert_nichts(self, monkeypatch):
        monkeypatch.setattr(settings, "alert_webhook_url", "")
        monkeypatch.setattr(settings, "telegram_bot_token", "")
        watchdog.notify("crash", "CRASH")  # kein Ziel → kein POST, kein Fehler

    def test_crash_event_loest_webhook_aus(self, monkeypatch):
        inst = _inst()
        calls = self._ziele(monkeypatch)
        instances.set_status(inst["id"], "running", None)
        watchdog.handle_event(_event("die", f"mc-inst-{inst['id']}", 137))
        assert len(calls) == 2
        assert any("CRASH" in (p.get("content") or p.get("text") or "")
                   for _, p in calls)


class _FakeEventsClient:
    def __init__(self, events):
        self._events = events
        self.filters = None

    def events(self, decode=True, filters=None):
        self.filters = dict(filters or {})
        return iter(self._events)


class TestWatchdogLoop:
    def test_schleife_verarbeitet_events_und_stoppt(self, monkeypatch):
        inst = _inst()
        client = _FakeEventsClient([
            _event("die", f"mc-inst-{inst['id']}", 1),
        ])
        from app import runtime
        monkeypatch.setattr(runtime, "_get_client", lambda: client)
        stop = threading.Event()
        thread = threading.Thread(target=watchdog.watchdog_loop,
                                  args=(stop, 0.05), daemon=True)
        thread.start()
        # warten, bis der Crash verarbeitet ist
        for _ in range(100):
            if instances.get_instance(inst["id"])["status"] == "error":
                break
            time.sleep(0.02)
        stop.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert client.filters == {"type": "container",
                                  "event": ["die", "start"]}
        assert instances.get_instance(inst["id"])["status"] == "error"

    def test_schleife_beendet_sofort_bei_stop(self):
        stop = threading.Event()
        stop.set()
        watchdog.watchdog_loop(stop, 0.05)  # kehrt ohne Docker zurück

    def test_ohne_docker_wartet_schleife(self, monkeypatch):
        from app import runtime

        def kein_docker():
            raise RuntimeError("Docker nicht erreichbar")

        monkeypatch.setattr(runtime, "_get_client", kein_docker)
        stop = threading.Event()

        def beende():
            stop.set()

        timer = threading.Timer(0.2, beende)
        timer.start()
        watchdog.watchdog_loop(stop, 0.05)  # warns once and waits until stop
        timer.join()
