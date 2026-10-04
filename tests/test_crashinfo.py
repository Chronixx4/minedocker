"""Tests für die Crash-Diagnose und die Absturzschleifen-Erkennung."""
import os
import shutil
import time

import pytest

from app import crashinfo, instances, runtime, watchdog
from app.config import settings


@pytest.fixture(autouse=True)
def _ruhe():
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    watchdog._expected_stops.clear()
    watchdog._crash_times.clear()
    watchdog._halted.clear()
    yield
    watchdog._expected_stops.clear()
    watchdog._crash_times.clear()
    watchdog._halted.clear()


def _keys(text):
    return [f["key"] for f in crashinfo.analyze(text)]


class TestMuster:
    def test_java_version_aus_klassendatei(self):
        text = ("java.lang.UnsupportedClassVersionError: net/minecraft/server/Main has been "
                "compiled by a more recent version of the Java Runtime (class file version "
                "65.0), this version of the Java Runtime only recognizes class file versions "
                "up to 61.0")
        finding = crashinfo.analyze(text)[0]
        assert finding["key"] == "java"
        assert "Java 21" in finding["hint"] and "Java 17" in finding["hint"]

    def test_alte_forge_braucht_java8(self):
        text = ("java.lang.ClassCastException: class jdk.internal.loader.ClassLoaders$AppClassLoader "
                "cannot be cast to class java.net.URLClassLoader")
        assert "Java 8" in crashinfo.analyze(text)[0]["hint"]

    def test_fehlende_abhaengigkeit_forge(self):
        text = """[main/ERROR] Missing or unsupported mandatory dependencies:
\tMod ID: 'architectury', Requested by: 'rei', Expected range: '[9.1,)', Actual version: '[MISSING]'
\tMod ID: 'cloth_config', Requested by: 'rei', Expected range: '[11,)', Actual version: '[MISSING]'
"""
        finding = crashinfo.analyze(text)[0]
        assert finding["key"] == "dependency"
        assert any("architectury" in e for e in finding["evidence"])
        assert len(finding["evidence"]) == 2

    def test_fehlende_abhaengigkeit_fabric(self):
        text = """Incompatible mods found!
 - Mod 'Sodium Extra' (sodium-extra) 0.5.4 requires any version of mod 'sodium', which is missing!
"""
        assert _keys(text) == ["dependency"]

    def test_client_mod_und_verdaechtige(self):
        text = """Attempted to load class net/minecraft/client/gui/screens/Screen for invalid dist DEDICATED_SERVER
Suspected Mod: Oculus (oculus), Version: 1.6.9
"""
        assert "client_mod" in _keys(text)
        assert crashinfo.suspected_mods(text) == ["Oculus (oculus), Version: 1.6.9"]

    @pytest.mark.parametrize("text,key", [
        ("java.lang.OutOfMemoryError: Java heap space", "memory"),
        ("**** FAILED TO BIND TO PORT!", "port"),
        ("You need to agree to the EULA in order to run the server", "eula"),
        ("Found duplicate mods: jei-1.20.1-15.2.jar, jei-1.20.1-15.3.jar", "duplicate"),
        ("org.spongepowered.asm.mixin.transformer.throwables.MixinTransformerError", "mixin"),
    ])
    def test_weitere_muster(self, text, key):
        assert key in _keys(text)

    def test_nichts_erkannt(self):
        assert crashinfo.analyze("[Server thread/INFO]: Done (3.2s)!") == []


class TestDiagnose:
    def test_crash_report_wird_gelesen(self, tmp_path):
        reports = tmp_path / "crash-reports"
        reports.mkdir()
        (reports / "crash-old.txt").write_text("java.lang.OutOfMemoryError", encoding="utf-8")
        os.utime(reports / "crash-old.txt", (time.time() - 3600, time.time() - 3600))
        (reports / "crash-new.txt").write_text("FAILED TO BIND TO PORT", encoding="utf-8")
        diag = crashinfo.diagnose(tmp_path, ["letzte Zeile"], exit_code=1)
        assert diag["crash_report"] == "crash-new.txt"
        assert diag["summary"] == "Port ist schon belegt"
        assert diag["log_tail"] == ["letzte Zeile"]

    def test_alter_report_zaehlt_nicht(self, tmp_path):
        reports = tmp_path / "crash-reports"
        reports.mkdir()
        (reports / "crash.txt").write_text("FAILED TO BIND TO PORT", encoding="utf-8")
        os.utime(reports / "crash.txt", (time.time() - 3600, time.time() - 3600))
        diag = crashinfo.diagnose(tmp_path, [], exit_code=1)
        assert diag["crash_report"] is None
        assert diag["findings"] == []
        assert diag["summary"] == "Ursache nicht erkannt"

    def test_exit_137_ist_speichermangel(self, tmp_path):
        diag = crashinfo.diagnose(tmp_path, [], exit_code=137)
        assert diag["findings"][0]["key"] == "memory"


def _die(inst, code=1):
    return {"Type": "container", "Action": "die",
            "Actor": {"Attributes": {"name": f"mc-inst-{inst['id']}", "exitCode": str(code)}}}


class TestWatchdog:
    def test_crash_speichert_diagnose(self, fake_docker):
        inst = instances.create_instance("Crash-Srv", "fabric", "1.21.4", accept_eula=True)
        runtime.start_instance(inst)
        container = fake_docker.containers._items[runtime.container_name(inst["id"])]
        container._logs = b"Starte\njava.lang.OutOfMemoryError: Java heap space"
        assert watchdog.handle_event(_die(inst)) == "crash"
        meta = instances.get_instance(inst["id"])
        assert meta["last_crash"]["summary"] == "Zu wenig Arbeitsspeicher"
        assert "Zu wenig Arbeitsspeicher" in meta["error"]

    def test_absturzschleife_wird_angehalten(self, fake_docker):
        inst = instances.create_instance("Loop-Srv", "fabric", "1.21.4", accept_eula=True)
        runtime.start_instance(inst)
        container = fake_docker.containers._items[runtime.container_name(inst["id"])]
        for _ in range(2):
            watchdog.handle_event(_die(inst))
            assert container.attrs["State"]["Running"] is True  # Docker startet neu
        watchdog.handle_event(_die(inst))
        assert container.attrs["State"]["Running"] is False
        assert "Absturzschleife" in instances.get_instance(inst["id"])["error"]
        # Neustart-Event dazwischen, dann das Stop-Event des Anhaltens
        watchdog.handle_event({"Type": "container", "Action": "start",
                               "Actor": {"Attributes": {"name": f"mc-inst-{inst['id']}"}}})
        assert watchdog.handle_event(_die(inst, 143)) == "stop"
        meta = instances.get_instance(inst["id"])
        assert meta["status"] == "error" and "Absturzschleife" in meta["error"]

    def test_manueller_start_setzt_zaehler_zurueck(self):
        watchdog._record_crash("x", now=1000.0)
        watchdog._record_crash("x", now=1001.0)
        watchdog.forget_crashes("x")
        assert watchdog._record_crash("x", now=1002.0) is False

    def test_alte_abstuerze_verfallen(self):
        watchdog._record_crash("y", now=0.0)
        watchdog._record_crash("y", now=10.0)
        assert watchdog._record_crash("y", now=10.0 + watchdog.CRASH_LOOP_WINDOW + 1) is False


class TestApi:
    def test_analysieren_und_ausblenden(self, client):
        inst = instances.create_instance("Api-Srv", "fabric", "1.21.4", accept_eula=True)
        reports = instances.instance_dir(inst["id"]) / "crash-reports"
        reports.mkdir()
        (reports / "crash.txt").write_text(
            "You need to agree to the EULA", encoding="utf-8")
        r = client.post(f"/api/instances/{inst['id']}/crash/analyze")
        assert r.status_code == 200, r.text
        assert r.json()["summary"] == "EULA nicht akzeptiert"
        assert client.get(f"/api/instances/{inst['id']}").json()["last_crash"]
        assert client.delete(f"/api/instances/{inst['id']}/crash").status_code == 200
        assert "last_crash" not in client.get(f"/api/instances/{inst['id']}").json()
