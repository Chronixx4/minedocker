"""Tests für den persistenten Statistik-Verlauf (SQLite) und den Live-Log-
Stream (SSE)."""
import shutil
import time

import pytest

from app import history as hist
from app import instances, runtime
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen():
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    yield


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "history.db"


# ---------------------------------------------------------------------------
# record / prune / query_series
# ---------------------------------------------------------------------------

class TestStore:
    def test_record_und_prune(self, db):
        now = int(time.time())
        rows = [
            (now, "container", "mc-inst-abc12345", 12.5, 2048.0, None, None),
            (now, "players", "abc12345", None, None, 3, 20),
        ]
        assert hist.record_samples(rows, db_path=db) == 2
        # Tick außerhalb der Retention wird gelöscht
        hist.record_samples([(now - 40 * 86400, "container", "x", 1.0, 1.0,
                              None, None)], db_path=db)
        assert hist.prune(retention_days=30, db_path=db) == 1
        result = hist.query_series(hours=24, db_path=db)
        assert result["bucket_seconds"] >= 60
        assert len(result["points"]) == 1
        point = result["points"][0]
        assert point["cpu"] == 12.5
        assert point["ram_mb"] == 2048.0
        assert point["players"] == 3
        assert result["instances"]["abc12345"] == [
            {"ts": point["ts"], "players": 3}]

    def test_buckettung_mittelt_ticks(self, db):
        # Auf Bucket-Grenze (300 s bei 24 h) ausrichten, damit beide Ticks
        # deterministisch im selben Bucket landen: cpu 10 und 20 → Mittel 15
        now = int(time.time()) - int(time.time()) % 300
        hist.record_samples([
            (now, "container", "c", 10.0, 100.0, None, None),
            (now + 10, "container", "c", 20.0, 300.0, None, None),
        ], db_path=db)
        result = hist.query_series(hours=24, db_path=db)
        assert len(result["points"]) == 1
        assert result["points"][0]["cpu"] == 15.0
        assert result["points"][0]["ram_mb"] == 200.0

    def test_leere_db(self, db):
        result = hist.query_series(hours=24, db_path=db)
        assert result["points"] == [] and result["instances"] == {}

    def test_player_ohne_pings_summiert_null(self, db):
        now = int(time.time())
        hist.record_samples([(now, "container", "c", 5.0, 50.0, None, None)],
                            db_path=db)
        result = hist.query_series(hours=1, db_path=db)
        assert result["points"][0]["players"] == 0


# ---------------------------------------------------------------------------
# sample_tick (Sampling-Durchlauf, gemockte Quellen)
# ---------------------------------------------------------------------------

class TestSampleTick:
    def test_tick_schreibt_container_und_spieler(self, db, monkeypatch):
        inst = instances.create_instance("Verlauf-Srv", "fabric", "1.21.4",
                                         accept_eula=True)
        monkeypatch.setattr(runtime, "docker_resources", lambda: {
            "found": True, "containers": [
                {"name": f"mc-inst-{inst['id']}", "cpu_percent": 7.5,
                 "ram_mb": 512.0, "ram_limit_mb": 2048.0}]})
        monkeypatch.setattr(hist, "instance_ping_target",
                            lambda inst_id: ("127.0.0.1", 59999))
        from app import minecraft
        monkeypatch.setattr(minecraft, "server_status",
                            lambda host, port, timeout=2.0:
                            {"online": True, "players": {"online": 2, "max": 20}})
        assert _tick(db, monkeypatch) == 2
        result_series = hist.query_series(hours=1, db_path=db)
        assert result_series["points"][0]["cpu"] == 7.5
        assert result_series["points"][0]["ram_mb"] == 512.0
        assert result_series["points"][0]["players"] == 2
        assert result_series["instances"][inst["id"]][0]["players"] == 2

    def test_ping_fehler_toleriert(self, db, monkeypatch):
        inst = instances.create_instance("Verlauf-Ping", "fabric", "1.21.4",
                                         accept_eula=True)
        monkeypatch.setattr(runtime, "docker_resources", lambda: {
            "found": True, "containers": [
                {"name": f"mc-inst-{inst['id']}", "cpu_percent": 1.0,
                 "ram_mb": 10.0, "ram_limit_mb": 100.0}]})
        monkeypatch.setattr(hist, "instance_ping_target",
                            lambda inst_id: ("127.0.0.1", 59999))
        from app import minecraft

        def boom(host, port, timeout=2.0):
            raise OSError("verbindungstot")

        monkeypatch.setattr(minecraft, "server_status", boom)
        result = _tick(db, monkeypatch)
        assert result == 1  # nur Container-Zeile, Spieler fehlt
        series = hist.query_series(hours=1, db_path=db)
        assert series["points"][0]["players"] == 0
        assert series["instances"] == {}

    def test_kein_docker_toleriert(self, db, monkeypatch):
        monkeypatch.setattr(runtime, "docker_resources",
                            lambda: {"found": False, "containers": []})
        assert _tick(db, monkeypatch) == 0


def _tick(db, monkeypatch):
    """sample_tick mit injeziertem DB-Pfad ausführen."""
    original_connect = hist._connect
    monkeypatch.setattr(hist, "_connect", lambda path=None: original_connect(db))
    return hist.sample_tick()


# ---------------------------------------------------------------------------
# API: /api/history + SSE-Log-Stream
# ---------------------------------------------------------------------------

class TestApi:
    def test_history_endpoint_leer(self, client):
        r = client.get("/api/history?hours=24")
        assert r.status_code == 200
        body = r.json()
        assert body["points"] == []
        assert body["bucket_seconds"] >= 60
        assert body["hours"] == 24

    def test_history_endpoint_mit_daten(self, client, db, monkeypatch):
        hist.record_samples([(int(time.time()), "container", "mc-inst-abc12345",
                              3.0, 100.0, None, None)], db_path=db)
        original_connect = hist._connect
        monkeypatch.setattr(hist, "_connect",
                            lambda path=None: original_connect(db))
        r = client.get("/api/history?hours=1")
        assert r.status_code == 200
        body = r.json()
        assert body["points"][0]["cpu"] == 3.0
        assert "names" in body

    def test_history_limitiert_hours(self, client):
        assert client.get("/api/history?hours=0").status_code == 422
        assert client.get("/api/history?hours=9999").status_code == 422

    def test_sse_stream_liefert_zeilen(self, client, monkeypatch):
        inst = instances.create_instance("Stream-Srv", "fabric", "1.21.4",
                                         accept_eula=True)

        def fake_follow(instance, tail=100):
            yield "erste zeile"
            yield "zweite zeile"

        monkeypatch.setattr(runtime, "follow_logs", fake_follow)
        with client.stream(
            "GET", f"/api/instances/{inst['id']}/logs/stream?tail=10"
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = b"".join(resp.iter_bytes()).decode("utf-8")
        assert 'data: "erste zeile"' in body
        assert 'data: "zweite zeile"' in body
        # JSON-kodiert: Zeilenumbruch im Log wird sicher übertragen
        monkeypatch.setattr(runtime, "follow_logs",
                            lambda i, tail=100: iter(["x\ny"]))
        with client.stream(
            "GET", f"/api/instances/{inst['id']}/logs/stream") as resp:
            body = b"".join(resp.iter_bytes()).decode("utf-8")
        assert "data: " + '"x\\ny"' in body

    def test_sse_stream_unbekannte_instanz_404(self, client):
        assert client.get("/api/instances/gibtsnicht/logs/stream").status_code == 404

    def test_sse_gestoppter_container_endet_sofort(self, client, instanz=None):
        inst = instances.create_instance("Stream-Stopp", "fabric", "1.21.4",
                                         accept_eula=True)
        # follow_logs ohne Container (kein Fake-Docker) → leeres Ende
        with client.stream(
            "GET", f"/api/instances/{inst['id']}/logs/stream") as resp:
            assert resp.status_code == 200
            body = b"".join(resp.iter_bytes()).decode("utf-8")
        assert body == ""


# ---------------------------------------------------------------------------
# runtime.follow_logs mit Fake-Container
# ---------------------------------------------------------------------------

class _StreamContainer:
    def __init__(self, chunks):
        self._chunks = chunks

    def logs(self, tail=None, follow=False, stream=False, timestamps=False):
        assert stream and follow
        return iter(self._chunks)


class TestFollowLogs:
    def test_decode_und_split(self, monkeypatch):
        container = _StreamContainer([b"zeile1\nzeile2\n", b"zeile3\n"])
        monkeypatch.setattr(runtime, "_get_client", lambda: object())
        monkeypatch.setattr(runtime, "_get_container",
                            lambda client, inst: container)
        lines = list(runtime.follow_logs({"id": "abc"}, tail=10))
        assert lines == ["zeile1", "zeile2", "zeile3"]

    def test_kein_container_leeres_ende(self, monkeypatch):
        monkeypatch.setattr(runtime, "_get_client", lambda: object())
        monkeypatch.setattr(runtime, "_get_container", lambda client, inst: None)
        assert list(runtime.follow_logs({"id": "abc"})) == []

    def test_docker_fehler_leeres_ende(self, monkeypatch):
        class Boom:
            def logs(self, **kwargs):
                raise RuntimeError("docker weg")

        monkeypatch.setattr(runtime, "_get_client", lambda: object())
        monkeypatch.setattr(runtime, "_get_container", lambda c, i: Boom())
        assert list(runtime.follow_logs({"id": "abc"})) == []
