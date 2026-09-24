"""Tests für den Scheduler: Zeitplan-Validierung, Auto-Start, geplanter
Neustart mit RCON-Vorwarnung, zeitgesteuerte Backups, Update-Check-Alerts,
Zustandsdatei und die API-Anbindung (PATCH schedule)."""
import datetime as dt
import json
import shutil
import threading

import pytest

from app import backups, instances, runtime, scheduler, updates, watchdog
from app.config import settings


def _inst(name="Sched-Test", schedule=None):
    inst = instances.create_instance(name, "fabric", "1.21.4", accept_eula=True)
    if schedule is not None:
        instances.update_schedule(inst["id"], schedule)
    return instances.get_instance(inst["id"])


@pytest.fixture(autouse=True)
def _sauber():
    """Instanz-Ordner + Scheduler-Zustand pro Test zurücksetzen."""
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    state = scheduler.state_path()
    if state.exists():
        state.unlink()
    scheduler.reset_for_tests()
    watchdog._expected_stops.clear()
    yield
    scheduler.reset_for_tests()
    watchdog._expected_stops.clear()
    if state.exists():
        state.unlink()


def _set_now(monkeypatch, when: dt.datetime):
    monkeypatch.setattr(scheduler, "_now", lambda: when)


def _state_entry(instance_id: str) -> dict:
    state = scheduler.load_state()
    return ((state.get("instances") or {}).get(instance_id)) or {}


# ---------------------------------------------------------------------------
# Zeitplan-Validierung + API
# ---------------------------------------------------------------------------

class TestValidierung:
    def test_update_schedule_merge(self):
        inst = _inst(schedule={"auto_start": True, "restart": {"enabled": True}})
        assert inst["schedule"]["auto_start"] is True
        assert inst["schedule"]["restart"]["enabled"] is True
        # Merge: nur Teilfelder ändern, Bestand bleibt
        inst = instances.update_schedule(inst["id"], {"restart": {"time": "05:30"}})
        sched = inst["instance"]["schedule"]
        assert sched["restart"]["enabled"] is True
        assert sched["restart"]["time"] == "05:30"

    def test_ungueltige_zeit_400(self):
        inst = _inst()
        with pytest.raises(Exception) as exc:
            instances.update_schedule(inst["id"], {"restart": {"time": "25:00"}})
        assert getattr(exc.value, "status_code", None) == 400

    def test_unbekanntes_feld_400(self):
        inst = _inst()
        with pytest.raises(Exception) as exc:
            instances.update_schedule(inst["id"], {"cron": "weekly"})
        assert getattr(exc.value, "status_code", None) == 400

    def test_bereich_fehler_400(self):
        inst = _inst()
        with pytest.raises(Exception) as exc:
            instances.update_schedule(inst["id"], {"backup": {"interval_hours": 0}})
        assert getattr(exc.value, "status_code", None) == 400
        with pytest.raises(Exception) as exc_w:
            instances.update_schedule(inst["id"], {"restart": {"warn_minutes": 31}})
        assert getattr(exc_w.value, "status_code", None) == 400

    def test_kein_objekt_400(self):
        inst = _inst()
        with pytest.raises(Exception) as exc:
            instances.update_schedule(inst["id"], {"backup": "nachts"})
        assert getattr(exc.value, "status_code", None) == 400

    def test_klon_uebernimmt_zeitplan(self):
        inst = _inst(schedule={"auto_start": True,
                               "backup": {"enabled": True, "interval_hours": 3}})
        klon = instances.clone_instance(inst["id"])
        assert klon["schedule"]["auto_start"] is True
        assert klon["schedule"]["backup"]["interval_hours"] == 3


class TestApiPatch:
    def test_patch_schedule(self, client):
        inst = _inst()
        r = client.patch(f"/api/instances/{inst['id']}", json={
            "schedule": {"auto_start": True,
                         "restart": {"enabled": True, "time": "04:00",
                                     "warn_minutes": 10}}})
        assert r.status_code == 200
        sched = r.json()["schedule"]
        assert sched["auto_start"] is True
        assert sched["restart"] == {"enabled": True, "time": "04:00",
                                    "warn_minutes": 10}

    def test_patch_nur_schedule_kein_leerer_body(self, client):
        inst = _inst()
        r = client.patch(f"/api/instances/{inst['id']}",
                         json={"schedule": {"auto_start": True}})
        assert r.status_code == 200
        # Leerer Body bleibt 400
        assert client.patch(f"/api/instances/{inst['id']}", json={}).status_code == 400

    def test_patch_schedule_ungueltig_422(self, client):
        inst = _inst()
        r = client.patch(f"/api/instances/{inst['id']}", json={
            "schedule": {"restart": {"time": "99:99"}}})
        assert r.status_code == 422
        r = client.patch(f"/api/instances/{inst['id']}", json={
            "schedule": {"backup": {"keep": 99}}})
        assert r.status_code == 422

    def test_patch_kombiniert_jvm_und_schedule(self, client):
        inst = _inst()
        r = client.patch(f"/api/instances/{inst['id']}", json={
            "jvm_opts": "-XX:+UseZGC",
            "schedule": {"auto_start": True}})
        assert r.status_code == 200
        assert r.json()["jvm_opts"] == "-XX:+UseZGC"
        assert r.json()["schedule"]["auto_start"] is True


# ---------------------------------------------------------------------------
# Auto-Start
# ---------------------------------------------------------------------------

class TestAutoStart:
    def test_startet_instanz_mit_flag(self, fake_docker):
        inst = _inst(schedule={"auto_start": True})
        assert scheduler.run_auto_start() == 1
        assert runtime.is_running(inst)
        assert instances.get_instance(inst["id"])["status"] == "running"
        # Zweiter Aufruf (Boot) → bereits laufend, kein Doppelstart
        assert scheduler.run_auto_start() == 0
        assert runtime.is_running(inst)

    def test_ohne_flag_unberuehrt(self, fake_docker):
        _inst(name="Ohne-Auto")
        assert scheduler.run_auto_start() == 0
        assert fake_docker.containers.run_kwargs is None

    def test_fehler_setzt_status_zurueck(self, fake_docker, monkeypatch):
        inst = _inst(schedule={"auto_start": True})
        monkeypatch.setattr(runtime, "start_instance",
                            lambda i: (_ for _ in ()).throw(RuntimeError("Docker weg")))
        assert scheduler.run_auto_start() == 0
        # Kein hängender 'starting'-Status
        assert instances.get_instance(inst["id"])["status"] in ("stopped", "error")


# ---------------------------------------------------------------------------
# Geplanter Neustart
# ---------------------------------------------------------------------------

class TestRestart:
    def _enabled(self, time="04:00", warn=5):
        return {"restart": {"enabled": True, "time": time, "warn_minutes": warn}}

    def test_vorwarnung_und_neustart(self, fake_docker, monkeypatch):
        inst = _inst(schedule=self._enabled())
        runtime.start_instance(inst)  # laufender Container (Fake)
        sent = []
        monkeypatch.setattr(scheduler, "_rcon_say",
                            lambda i, msg: sent.append(msg) or True)

        # 03:57 → nur die 5-Minuten-Warnung
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 3, 57, 0))
        counts = scheduler.tick()
        assert counts["warn"] == 1
        assert counts["restart"] == 0
        assert len(sent) == 1 and "5 Minute" in sent[0]
        # Gleicher Tick erneut → keine Doppelwarnung
        assert scheduler.tick()["warn"] == 0
        # 03:59 → 1-Minuten-Warnung
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 3, 59, 0))
        assert scheduler.tick()["warn"] == 1
        assert any("1 Minute" in m for m in sent)
        # 04:00 → Neustart, Container läuft weiter (Fake)
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 4, 0, 0))
        counts = scheduler.tick()
        assert counts["restart"] == 1
        assert runtime.is_running(inst)
        assert _state_entry(inst["id"])["restart_last"] == "2026-09-21"
        # Gleicher Tag → kein erneuter Neustart
        assert scheduler.tick()["restart"] == 0
        # Nächster Tag zur Zeit → wieder Neustart
        _set_now(monkeypatch, dt.datetime(2026, 9, 22, 4, 0, 0))
        assert scheduler.tick()["restart"] == 1

    def test_kein_neustart_wenn_gestoppt(self, fake_docker, monkeypatch):
        inst = _inst(schedule=self._enabled())
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 4, 0, 0))
        counts = scheduler.tick()
        assert counts["restart"] == 1  # markiert, aber ohne Container-Aktion
        assert fake_docker.containers.run_kwargs is None
        assert instances.get_instance(inst["id"])["status"] == "stopped"
        assert _state_entry(inst["id"])["restart_last"] == "2026-09-21"

    def test_verpasster_termin_wird_uebersprungen(self, monkeypatch):
        inst = _inst(schedule=self._enabled())
        # 10:00 Uhr (Termin > 30 min vorbei) → übersprungen, für heute markiert
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 10, 0, 0))
        assert scheduler.tick()["restart"] == 1
        assert _state_entry(inst["id"])["restart_last"] == "2026-09-21"
        # Innerhalb des Fensters (20 min nach Termin) wäre er gefeuert worden
        inst2 = _inst(name="Sched-Frisch", schedule=self._enabled())
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 4, 20, 0))
        assert scheduler.tick()["restart"] == 1
        assert _state_entry(inst2["id"])["restart_last"] == "2026-09-21"

    def test_ohne_vorwarnung(self, fake_docker, monkeypatch):
        inst = _inst(schedule=self._enabled(warn=0))
        runtime.start_instance(inst)
        sent = []
        monkeypatch.setattr(scheduler, "_rcon_say",
                            lambda i, msg: sent.append(msg) or True)
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 3, 57, 0))
        counts = scheduler.tick()
        assert counts["warn"] == 0 and sent == []
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 4, 0, 0))
        assert scheduler.tick()["restart"] == 1

    def test_ungueltige_zeit_ignoriert(self, monkeypatch):
        inst = instances.create_instance("Sched-Kaputt", "fabric", "1.21.4",
                                         accept_eula=True)
        inst["schedule"] = {"restart": {"enabled": True, "time": "keine-uhrzeit"}}
        instances.update_instance(inst)
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 4, 0, 0))
        counts = scheduler.tick()
        assert counts["restart"] == 0 and counts["warn"] == 0

    def test_restarting_mit_rcon_weg_laeuft_trotzdem(self, fake_docker, monkeypatch):
        """RCON-Fehler dürfen Warnung/Neustart nicht blockieren."""
        inst = _inst(schedule=self._enabled())
        runtime.start_instance(inst)
        monkeypatch.setattr(scheduler, "_rcon_say", lambda i, msg: False)
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 3, 57, 0))
        assert scheduler.tick()["warn"] == 0  # nicht gesendet, nicht markiert
        monkeypatch.setattr(scheduler, "_rcon_say",
                            lambda i, msg: True)  # RCON wieder da
        assert scheduler.tick()["warn"] == 1
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 4, 0, 0))
        assert scheduler.tick()["restart"] == 1


# ---------------------------------------------------------------------------
# Geplanter Stopp (Spiegel des Neustarts: Vorwarnung, Termin, Nachhol-Fenster)
# ---------------------------------------------------------------------------

class TestStop:
    def _enabled(self, time="23:00", warn=5):
        return {"stop": {"enabled": True, "time": time, "warn_minutes": warn}}

    def test_vorwarnung_und_stopp(self, fake_docker, monkeypatch):
        inst = _inst(schedule=self._enabled())
        runtime.start_instance(inst)  # laufender Container (Fake)
        sent = []
        monkeypatch.setattr(scheduler, "_rcon_say",
                            lambda i, msg: sent.append(msg) or True)

        # 22:55 → nur die 5-Minuten-Warnung
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 22, 55, 0))
        counts = scheduler.tick()
        assert counts["warn"] == 1 and counts["stop"] == 0
        assert len(sent) == 1 and "stoppt in 5 Minute" in sent[0]
        # Gleicher Tick erneut → keine Doppelwarnung
        assert scheduler.tick()["warn"] == 0
        # 22:59 → 1-Minuten-Warnung
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 22, 59, 0))
        assert scheduler.tick()["warn"] == 1
        assert any("1 Minute" in m for m in sent)
        # 23:00 → Stopp, Container ist aus
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 23, 0, 0))
        counts = scheduler.tick()
        assert counts["stop"] == 1
        assert not runtime.is_running(inst)
        assert instances.get_instance(inst["id"])["status"] == "stopped"
        assert _state_entry(inst["id"])["stop_last"] == "2026-09-21"
        # Gleicher Tag → kein erneuter Stopp
        assert scheduler.tick()["stop"] == 0
        # Nächster Tag zur Zeit → wieder Stopp (Instanz läuft nicht → nur Markierung)
        _set_now(monkeypatch, dt.datetime(2026, 9, 22, 23, 0, 0))
        assert scheduler.tick()["stop"] == 1

    def test_kein_stopp_wenn_gestoppt(self, fake_docker, monkeypatch):
        inst = _inst(schedule=self._enabled())
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 23, 0, 0))
        counts = scheduler.tick()
        assert counts["stop"] == 1  # markiert, aber ohne Container-Aktion
        assert fake_docker.containers.run_kwargs is None
        assert instances.get_instance(inst["id"])["status"] == "stopped"
        assert _state_entry(inst["id"])["stop_last"] == "2026-09-21"

    def test_verpasster_termin_wird_uebersprungen(self, monkeypatch):
        inst = _inst(schedule=self._enabled(time="04:00"))
        # Termin > 30 min vorbei → übersprungen, für heute markiert
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 10, 0, 0))
        assert scheduler.tick()["stop"] == 1
        assert _state_entry(inst["id"])["stop_last"] == "2026-09-21"

    def test_stopp_binnen_nachhol_fenster(self, fake_docker, monkeypatch):
        inst = _inst(schedule=self._enabled())
        runtime.start_instance(inst)
        # 20 min nach Termin → nachgeholt
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 23, 20, 0))
        assert scheduler.tick()["stop"] == 1
        assert not runtime.is_running(inst)
        assert instances.get_instance(inst["id"])["status"] == "stopped"

    def test_ohne_vorwarnung(self, fake_docker, monkeypatch):
        inst = _inst(schedule=self._enabled(warn=0))
        runtime.start_instance(inst)
        sent = []
        monkeypatch.setattr(scheduler, "_rcon_say",
                            lambda i, msg: sent.append(msg) or True)
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 22, 55, 0))
        counts = scheduler.tick()
        assert counts["warn"] == 0 and sent == []
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 23, 0, 0))
        assert scheduler.tick()["stop"] == 1
        assert not runtime.is_running(inst)

    def test_ungueltige_zeit_ignoriert(self, monkeypatch):
        inst = instances.create_instance("Sched-Stopp-Kaputt", "fabric",
                                         "1.21.4", accept_eula=True)
        inst["schedule"] = {"stop": {"enabled": True, "time": "keine-uhrzeit"}}
        instances.update_instance(inst)
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 23, 0, 0))
        counts = scheduler.tick()
        assert counts["stop"] == 0 and counts["warn"] == 0

    def test_stopp_fehler_setzt_error_und_alert(self, fake_docker, monkeypatch):
        inst = _inst(schedule=self._enabled())
        runtime.start_instance(inst)
        alerts = []
        monkeypatch.setattr(watchdog, "notify",
                            lambda event, msg: alerts.append(event))

        def kaputt(i):
            raise RuntimeError("Docker weg")

        monkeypatch.setattr(runtime, "stop_instance", kaputt)
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 23, 0, 0))
        scheduler.tick()
        assert instances.get_instance(inst["id"])["status"] == "error"
        assert "crash" in alerts

    def test_klon_uebernimmt_stopp(self):
        inst = _inst(schedule=self._enabled())
        klon = instances.clone_instance(inst["id"])
        assert klon["schedule"]["stop"] == {"enabled": True, "time": "23:00",
                                            "warn_minutes": 5}

    def test_api_patch_stop(self, client):
        inst = _inst()
        r = client.patch(f"/api/instances/{inst['id']}", json={
            "schedule": {"stop": {"enabled": True, "time": "22:30",
                                  "warn_minutes": 3}}})
        assert r.status_code == 200
        sched = r.json()["schedule"]
        assert sched["stop"] == {"enabled": True, "time": "22:30",
                                 "warn_minutes": 3}
        # Bereichsfehler (Pydantic-Deckel im API-Modell)
        r = client.patch(f"/api/instances/{inst['id']}", json={
            "schedule": {"stop": {"warn_minutes": 31}}})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Zeitgesteuerte Backups
# ---------------------------------------------------------------------------

class TestBackup:
    def _enabled(self, hours=1, keep=2):
        return {"backup": {"enabled": True, "interval_hours": hours, "keep": keep}}

    def test_erstes_tick_setzt_nur_faelligkeit(self, monkeypatch):
        inst = _inst(schedule=self._enabled())
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 4, 0, 0))
        counts = scheduler.tick()
        assert counts["backup"] == 0
        assert backups.list_backups(inst["id"]) == []
        assert _state_entry(inst["id"])["backup_last"] is not None

    def test_backup_nach_intervall_und_rotation(self, monkeypatch):
        inst = _inst(schedule=self._enabled(hours=1, keep=1))
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 4, 0, 0))
        scheduler.tick()
        # 2 Stunden später → fällig
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 6, 0, 0))
        assert scheduler.tick()["backup"] == 1
        names = [b["name"] for b in backups.list_backups(inst["id"])]
        assert len(names) == 1 and names[0].startswith("scheduled-")
        # Sofort erneut → Intervall noch nicht erreicht
        assert scheduler.tick()["backup"] == 0
        # Weitere 2 Stunden (keep=1) → altes scheduled-Backup rotiert weg
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 8, 0, 0))
        assert scheduler.tick()["backup"] == 1
        names = [b["name"] for b in backups.list_backups(inst["id"])]
        assert len(names) == 1

    def test_deaktiviert_kein_backup(self, monkeypatch):
        inst = _inst()
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 4, 0, 0))
        assert scheduler.tick()["backup"] == 0
        assert "backup_last" not in _state_entry(inst["id"])


# ---------------------------------------------------------------------------
# Geplanter Update-Check
# ---------------------------------------------------------------------------

class TestUpdateCheck:
    def _enabled(self, hours=1):
        return {"update_check": {"enabled": True, "interval_hours": hours}}

    def test_check_und_alert(self, fake_docker, monkeypatch):
        inst = _inst(schedule=self._enabled())
        calls = []
        monkeypatch.setattr(watchdog, "notify", lambda event, msg: calls.append((event, msg)))
        checks = []

        async def fake_check(instance_id):
            checks.append(instance_id)
            return {"items": [{"filename": "a.jar", "status": updates.S_UPDATE},
                              {"filename": "b.jar", "status": updates.S_CURRENT}],
                    "checked": 2, "updatable": 1, "curseforge_enabled": False}

        monkeypatch.setattr(updates, "check_updates", fake_check)
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 4, 0, 0))
        assert scheduler.tick()["update_check"] == 0  # Fälligkeit gesetzt
        assert checks == []
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 6, 0, 0))
        assert scheduler.tick()["update_check"] == 1
        assert checks == [inst["id"]]
        assert len(calls) == 1
        event, msg = calls[0]
        assert event == "update"
        assert "a.jar" in msg and inst["name"] in msg
        # Sofort erneut → Intervall verhindert zweiten Check
        assert scheduler.tick()["update_check"] == 0
        assert len(checks) == 1

    def test_kein_alert_ohne_updates(self, fake_docker, monkeypatch):
        _inst(schedule=self._enabled())
        calls = []
        monkeypatch.setattr(watchdog, "notify", lambda event, msg: calls.append(event))

        async def fake_check(instance_id):
            return {"items": [], "checked": 0, "updatable": 0,
                    "curseforge_enabled": False}

        monkeypatch.setattr(updates, "check_updates", fake_check)
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 4, 0, 0))
        scheduler.tick()
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 6, 0, 0))
        assert scheduler.tick()["update_check"] == 1
        assert calls == []

    def test_check_fehler_blockiert_nicht(self, fake_docker, monkeypatch):
        _inst(schedule=self._enabled())

        async def boom(instance_id):
            raise RuntimeError("Anbieter weg")

        monkeypatch.setattr(updates, "check_updates", boom)
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 4, 0, 0))
        scheduler.tick()
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 6, 0, 0))
        # Fehler wird nur geloggt — Tick zählt den Check nicht als Erfolg
        assert scheduler.tick()["update_check"] == 0
        assert scheduler.load_state()  # Zustand existiert weiterhin
        # Intervall ist trotz Fehler weitergelaufen (kein Retry-Spam)
        assert scheduler.tick()["update_check"] == 0


# ---------------------------------------------------------------------------
# Zustandsdatei + Loop
# ---------------------------------------------------------------------------

class TestState:
    def test_state_undef_oder_defekt(self):
        assert scheduler.load_state() == {}
        scheduler.state_path().write_text("kaputt{", encoding="utf-8")
        assert scheduler.load_state() == {}

    def test_tick_räumt_verwaiste_eintraege(self, monkeypatch):
        inst = _inst(schedule={"auto_start": True})
        scheduler.state_path().write_text(json.dumps(
            {"instances": {inst["id"]: {}, "tot-abcd": {"backup_last": 1}}}),
            encoding="utf-8")
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 4, 0, 0))
        scheduler.tick()
        state = scheduler.load_state()["instances"]
        assert inst["id"] in state
        assert "tot-abcd" not in state

    def test_state_überlebt_tick_grenzen(self, monkeypatch):
        _inst(schedule={"restart": {"enabled": True, "time": "04:00"}})
        _set_now(monkeypatch, dt.datetime(2026, 9, 21, 4, 0, 0))
        scheduler.tick()
        scheduler.save_state({})  # Zustand "verloren" (Neustart-Simulation)
        # Ohne restart_last würde erneut gefeuert — Verhalten ist bewusst:
        # nach Zustandsverlust zählt der Tag neu (keine dauerhafte Sperre)
        counts = scheduler.tick()
        assert counts["restart"] == 1

    def test_loop_bei_gesetztem_stop(self):
        stop = threading.Event()
        stop.set()
        thread = threading.Thread(target=scheduler.scheduler_loop, args=(stop,),
                                  daemon=True)
        thread.start()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
