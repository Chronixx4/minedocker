"""Tests für die Spielzeit je Spieler: Session-Logik (Sampler), Persistenz
(player_sessions/player_daily), Leaderboard-Query und /api/players/playtime."""
import shutil
import time

import pytest

from app import history as hist
from app import instances, runtime
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen_und_sessions():
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    hist.reset_sessions_for_tests()
    yield
    hist.reset_sessions_for_tests()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Test-DB + global gepatchtes _connect, damit auch die internen
    Flush-Aufrufe (update_sessions/close_all_sessions) in die Test-DB
    schreiben statt in die Standard-Datei."""
    path = tmp_path / "history.db"
    original_connect = hist._connect
    monkeypatch.setattr(hist, "_connect", lambda p=None: original_connect(path))
    return path


# Fixe Basiszeit (Mitte Juni, keine DST-Grenze): 2026-06-15 12:00:00 lokal
BASE_TS = int(time.mktime(time.strptime("2026-06-15 12:00:00", "%Y-%m-%d %H:%M:%S")))


class TestSessionLogik:
    def test_sicht_oeffnet_und_verlaengert(self, db):
        closed = hist.update_sessions(BASE_TS, "inst1", ["Alice"], 30)
        assert closed == []
        assert hist.open_sessions() == [("inst1", "Alice",
                                         {"start": BASE_TS,
                                          "last_seen": BASE_TS,
                                          "interval": 30})]
        closed = hist.update_sessions(BASE_TS + 30, "inst1", ["Alice"], 30)
        assert closed == []
        assert hist.open_sessions()[0][2]["last_seen"] == BASE_TS + 30
        hist.reset_sessions_for_tests()

    def test_zwei_misses_schliessen_und_flusht(self, db):
        hist.update_sessions(BASE_TS, "inst1", ["Alice"], 30)
        hist.update_sessions(BASE_TS + 30, "inst1", ["Alice"], 30)
        # zwei Ticks ohne Alice → beim zweiten wird geschlossen (end=letzte Sicht)
        assert hist.update_sessions(BASE_TS + 60, "inst1", [], 30) == []
        closed = hist.update_sessions(BASE_TS + 90, "inst1", [], 30)
        assert len(closed) == 1
        s = closed[0]
        assert s["player"] == "Alice"
        assert s["start"] == BASE_TS
        assert s["end"] == BASE_TS + 30
        assert s["seconds"] == (BASE_TS + 30 - BASE_TS) + 30  # 60
        # Persistenz: player_sessions + player_daily gefüllt
        with hist._connect(db) as conn:
            rows = conn.execute(
                "SELECT instance, player, start_ts, end_ts, seconds "
                "FROM player_sessions").fetchall()
            daily = conn.execute(
                "SELECT day, instance, player, seconds FROM player_daily"
                ).fetchall()
        assert rows == [("inst1", "Alice", BASE_TS, BASE_TS + 30, 60)]
        assert daily == [(time.strftime("%Y-%m-%d", time.localtime(BASE_TS)),
                          "inst1", "Alice", 60)]
        assert hist.open_sessions() == []

    def test_zu_grosse_luecke_schliesst_und_oeffnet_neu(self, db):
        hist.update_sessions(BASE_TS, "inst1", ["Bob"], 30)
        # Sampler-Pause: nächster Sichtkontakt erst 5 Intervalle später
        closed = hist.update_sessions(BASE_TS + 150, "inst1", ["Bob"], 30)
        assert len(closed) == 1
        assert closed[0]["end"] == BASE_TS  # end = letzte Sicht
        assert closed[0]["seconds"] == 30  # (0) + Intervall
        # neue Session ab jetzt
        assert hist.open_sessions()[0][2]["start"] == BASE_TS + 150

    def test_instanz_stopp_schliesst_sofort(self, db):
        hist.update_sessions(BASE_TS, "inst1", ["Alice"], 30)
        closed = hist.apply_tick(BASE_TS + 30, {}, set(), 30)
        assert len(closed) == 1
        assert closed[0]["instance"] == "inst1"
        assert hist.open_sessions() == []
        hist.flush_sessions(closed, db_path=db)
        with hist._connect(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM player_sessions"
                                ).fetchone()[0] == 1

    def test_close_all_beim_shutdown(self, db):
        hist.update_sessions(BASE_TS, "inst1", ["Alice", "Bob"], 30)
        assert hist.close_all_sessions(now_ts=BASE_TS + 120) == 2
        assert hist.open_sessions() == []
        with hist._connect(db) as conn:
            names = {r[0] for r in conn.execute(
                "SELECT player FROM player_sessions").fetchall()}
        assert names == {"Alice", "Bob"}

    def test_max_namen_gekappt(self, db):
        names = [f"Spieler{i:03d}" for i in range(300)]
        hist.update_sessions(BASE_TS, "inst1", names, 30)
        assert len(hist.open_sessions()) == 200


class TestTagAufteilung:
    def test_split_days_intra_tag(self):
        parts = hist._split_days(BASE_TS, BASE_TS + 60, 90)
        assert parts == [(time.strftime("%Y-%m-%d", time.localtime(BASE_TS)), 90)]

    def test_split_days_ueber_mitternacht(self, db):
        start = int(time.mktime(time.strptime(
            "2026-06-15 23:59:50", "%Y-%m-%d %H:%M:%S")))
        end = start + 20  # 00:00:10 am Folgetag
        parts = hist._split_days(start, end, 50)
        day1, day2 = time.strftime("%Y-%m-%d", time.localtime(start)), \
            time.strftime("%Y-%m-%d", time.localtime(end))
        assert dict(parts) == {day1: 25, day2: 25}
        # Session über Mitternacht: vor und nach dem Tageswechsel gesehen
        hist.update_sessions(start, "inst1", ["Nacht"], 30)
        hist.update_sessions(end, "inst1", ["Nacht"], 30)
        closed = hist.apply_tick(end + 60, {}, set(), 30)
        hist.flush_sessions(closed, db_path=db)
        with hist._connect(db) as conn:
            rows = dict(conn.execute(
                "SELECT day, seconds FROM player_daily").fetchall())
        assert set(rows) == {day1, day2}
        assert sum(rows.values()) == (end - start) + 30

    def test_split_bei_tageswechsel_rundungsrest(self):
        start = int(time.mktime(time.strptime(
            "2026-06-15 22:00:00", "%Y-%m-%d %H:%M:%S")))
        end = int(time.mktime(time.strptime(
            "2026-06-16 02:00:00", "%Y-%m-%d %H:%M:%S")))
        parts = dict(hist._split_days(start, end, 14337))
        assert sum(parts.values()) == 14337
        assert len(parts) == 2


class TestQueryPlaytime:
    def test_leeres_ergebnis(self, db):
        result = hist.query_playtime("24", db_path=db, now=BASE_TS)
        assert result["players"] == [] and result["hours"] == 24

    def test_daily_und_last_seen(self, db):
        day = time.strftime("%Y-%m-%d", time.localtime(BASE_TS - 3600))
        with hist._connect(db) as conn:
            conn.executemany(
                "INSERT INTO player_daily (day, instance, player, seconds) "
                "VALUES (?,?,?,?)",
                [(day, "inst1", "Alice", 3600),
                 (day, "inst2", "Alice", 120),
                 (day, "inst1", "Bob", 60)])
        result = hist.query_playtime("24", db_path=db, now=BASE_TS)
        alice, bob = result["players"]
        assert alice["player"] == "Alice" and bob["player"] == "Bob"
        assert alice["seconds"] == 3720
        assert alice["per_instance"] == {"inst1": 3600, "inst2": 120}

    def test_offene_session_teilzeit_und_filter(self, db):
        hist.update_sessions(BASE_TS - 600, "inst1", ["Alice"], 30)
        hist.update_sessions(BASE_TS - 600, "inst2", ["Bob"], 30)
        result = hist.query_playtime("24", db_path=db, now=BASE_TS)
        assert {p["player"] for p in result["players"]} == {"Alice", "Bob"}
        alice = next(p for p in result["players"] if p["player"] == "Alice")
        assert alice["seconds"] == 600
        assert alice["last_seen"] == BASE_TS - 600  # nur einmal gesehen
        # Instanz-Filter
        only = hist.query_playtime("24", instance="inst1", db_path=db,
                                   now=BASE_TS)
        assert [p["player"] for p in only["players"]] == ["Alice"]

    def test_zeitfenster_schneidet_alt_ab(self, db):
        old_day = time.strftime("%Y-%m-%d", time.localtime(BASE_TS - 90 * 86400))
        with hist._connect(db) as conn:
            conn.execute(
                "INSERT INTO player_daily (day, instance, player, seconds) "
                "VALUES (?,?,?,?)", (old_day, "inst1", "Alt", 999999))
        result = hist.query_playtime("24", db_path=db, now=BASE_TS)
        assert result["players"] == []
        result_all = hist.query_playtime("all", db_path=db, now=BASE_TS)
        assert result_all["players"][0]["player"] == "Alt"
        assert result_all["hours"] == "all"


class TestSampleTickIntegration:
    def test_tick_erfasst_namen_als_session(self, db, monkeypatch):
        inst = instances.create_instance("Spielzeit-Srv", "fabric", "1.21.4",
                                         accept_eula=True)
        monkeypatch.setattr(runtime, "docker_resources", lambda: {
            "found": True, "containers": [
                {"name": f"mc-inst-{inst['id']}", "cpu_percent": 3.0,
                 "ram_mb": 256.0, "ram_limit_mb": 2048.0}]})
        monkeypatch.setattr(hist, "instance_ping_target",
                            lambda inst_id: ("127.0.0.1", 59999))
        from app import minecraft
        online_state = {"names": ["Alice", "Bob"]}

        def fake_ping(host, port, timeout=2.0):
            if not online_state["names"]:
                return {"online": True, "players": {"online": 0, "max": 20}}
            return {"online": True, "players": {
                "online": len(online_state["names"]), "max": 20,
                "sample": [{"name": n, "uuid": "u"} for n in online_state["names"]]}}

        monkeypatch.setattr(minecraft, "server_status", fake_ping)
        original_connect = hist._connect
        monkeypatch.setattr(hist, "_connect",
                            lambda path=None: original_connect(db))
        assert hist.sample_tick(now=BASE_TS) == 2
        open_now = hist.open_sessions()
        assert {(i, p) for i, p, _ in open_now} == {
            (inst["id"], "Alice"), (inst["id"], "Bob")}
        # weiterer Tick mit Sicht verlängert, 2 Misses → geschlossen und in der DB
        hist.sample_tick(now=BASE_TS + 30)
        online_state["names"] = []
        hist.sample_tick(now=BASE_TS + 60)
        hist.sample_tick(now=BASE_TS + 90)
        with hist._connect(db) as conn:
            rows = conn.execute(
                "SELECT player, seconds FROM player_sessions").fetchall()
        assert {r[0] for r in rows} == {"Alice", "Bob"}
        assert all(r[1] == 30 + 30 for r in rows)  # (last_seen-start) + Intervall

    def test_rcon_fallback_bei_leerem_sample(self, db, monkeypatch):
        inst = instances.create_instance("Rcon-Fallback", "fabric", "1.21.4",
                                         accept_eula=True)
        monkeypatch.setattr(runtime, "docker_resources", lambda: {
            "found": True, "containers": [
                {"name": f"mc-inst-{inst['id']}", "cpu_percent": 0.5,
                 "ram_mb": 100.0, "ram_limit_mb": 100.0}]})
        monkeypatch.setattr(hist, "instance_ping_target",
                            lambda inst_id: ("127.0.0.1", 59999))
        from app import minecraft
        monkeypatch.setattr(minecraft, "server_status",
                            lambda host, port, timeout=2.0:
                            {"online": True,
                             "players": {"online": 1, "max": 20}})  # kein sample
        monkeypatch.setattr(hist, "_rcon_list_names",
                            lambda i: ["Carol"])
        original_connect = hist._connect
        monkeypatch.setattr(hist, "_connect",
                            lambda path=None: original_connect(db))
        hist.sample_tick(now=BASE_TS)
        assert {(i, p) for i, p, _ in hist.open_sessions()} == {
            (inst["id"], "Carol")}


class TestPlaytimeRoute:
    def test_leer_und_validierung(self, client, db, monkeypatch):
        r = client.get("/api/players/playtime")
        assert r.status_code == 200
        assert r.json()["players"] == []
        assert client.get("/api/players/playtime?hours=all").status_code == 200
        assert client.get("/api/players/playtime?hours=0").status_code == 422
        assert client.get("/api/players/playtime?hours=9999").status_code == 422
        assert client.get("/api/players/playtime?instance=gibtsnicht"
                          ).status_code == 404

    def test_leaderboard_ueber_route(self, client, db, monkeypatch):
        # Route nutzt die reale Uhrzeit → Tages-Aggregat auf heute legen
        day = time.strftime("%Y-%m-%d", time.localtime())
        with hist._connect(db) as conn:
            conn.executemany(
                "INSERT INTO player_daily (day, instance, player, seconds) "
                "VALUES (?,?,?,?)",
                [(day, "inst1", "Alice", 3600), (day, "inst1", "Bob", 120)])
        r = client.get("/api/players/playtime?hours=24")
        assert r.status_code == 200
        players = r.json()["players"]
        assert [p["player"] for p in players] == ["Alice", "Bob"]
        assert players[0]["seconds"] == 3600
        assert players[0]["per_instance"] == {"inst1": 3600}
