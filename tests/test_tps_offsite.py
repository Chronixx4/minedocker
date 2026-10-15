"""Tests für TPS-Parser, Lag-/Backup-Alerts und den WebDAV-Off-Site-Upload."""
import pytest

from app import offsite, scheduler, tps, watchdog
from app.config import settings


class TestParse:
    def test_vanilla_tick_query(self):
        out = "Target tick rate: 20.0 per second.\nAverage time per tick: 25.0ms (Target: 50.0ms)"
        assert tps.parse(out) == {"tps": 20.0, "mspt": 25.0}

    def test_vanilla_langsam(self):
        assert tps.parse("Average time per tick: 100.0ms")["tps"] == 10.0

    def test_paper(self):
        out = "§6TPS from last 1m, 5m, 15m: §a*19.8, §a20.0, §a20.0"
        assert tps.parse(out)["tps"] == 19.8

    def test_forge(self):
        out = "Overall: Mean tick time: 12.5 ms. Mean TPS: 20.0"
        assert tps.parse(out) == {"tps": 20.0, "mspt": 12.5}

    def test_unbekannt(self):
        assert tps.parse("Unknown or incomplete command") is None

    def test_kommando_je_loader(self):
        assert tps.command_for("paper") == "tps"
        assert tps.command_for("neoforge") == "neoforge tps"
        assert tps.command_for("fabric") == "tick query"

    def test_lag(self):
        assert tps.is_lagging({"tps": 12.0, "mspt": 80.0})
        assert not tps.is_lagging({"tps": 20.0, "mspt": 10.0})
        assert not tps.is_lagging(None)


class TestAlerts:
    def test_backup_fehler_alarmiert(self, monkeypatch):
        sent = []
        monkeypatch.setattr(watchdog, "notify", lambda ev, msg: sent.append(ev))

        def boom(*a, **k):
            raise OSError("voll")

        monkeypatch.setattr(scheduler.backups, "scheduled_backup", boom)
        import datetime as dt
        inst = {"id": "x", "name": "X", "schedule": {"backup": {
            "enabled": True, "interval_hours": 1, "keep": 3}}}
        entry = {"backup_last": 0}
        with pytest.raises(OSError):
            scheduler.handle_backup(inst, entry, dt.datetime.now())
        assert sent == ["backup_failed"]

    def test_offsite_upload(self, monkeypatch, tmp_path):
        calls = []

        class Resp:
            status_code = 201

        class FakeClient:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def request(self, method, url): calls.append((method, url))
            def put(self, url, content):
                calls.append(("PUT", url, content.read()))
                return Resp()

        monkeypatch.setattr(offsite.httpx, "Client", FakeClient)
        monkeypatch.setattr(settings, "backup_webdav_url", "https://dav.example/mc/")
        f = tmp_path / "scheduled-1.tar.gz"
        f.write_bytes(b"data")
        assert offsite.enabled()
        offsite.upload("abc", f)
        assert ("PUT", "https://dav.example/mc/abc/scheduled-1.tar.gz", b"data") in calls
