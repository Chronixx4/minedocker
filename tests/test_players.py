"""Tests für die Spieler-Übersicht (app/players.py, app/nbt.py + Routen)."""
import json
import shutil

import pytest

from app import history, instances, nbt
from app import players as pl
from app.config import settings

U1 = "11111111-2222-3333-4444-555555555555"
U2 = "66666666-7777-8888-9999-000000000000"


@pytest.fixture(autouse=True)
def _leere_instanzen():
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    history.reset_sessions_for_tests()
    yield
    history.reset_sessions_for_tests()


@pytest.fixture()
def server():
    inst = instances.create_instance("Spieler-Srv", "fabric", "1.21.4", accept_eula=True)
    base = instances.instance_dir(inst["id"])
    world = base / "world"
    for sub in ("stats", "advancements", "playerdata"):
        (world / sub).mkdir(parents=True)
    (world / "level.dat").write_bytes(nbt.dumps({"Data": {"raining": True, "thundering": False}}))
    (base / "usercache.json").write_text(json.dumps([
        {"name": "Alex", "uuid": U1, "expiresOn": "x"},
        {"name": "Steve", "uuid": U2, "expiresOn": "x"}]))
    (base / "ops.json").write_text(json.dumps([{"uuid": U1, "name": "Alex", "level": 4}]))
    (base / "whitelist.json").write_text(json.dumps([{"uuid": U1, "name": "Alex"},
                                                     {"uuid": U2, "name": "Steve"}]))
    (base / "banned-players.json").write_text(json.dumps([{"uuid": U2, "name": "Steve"}]))
    (world / "stats" / f"{U1}.json").write_text(json.dumps({"stats": {
        "minecraft:custom": {"minecraft:play_time": 72000, "minecraft:deaths": 1,
                             "minecraft:mob_kills": 12522, "minecraft:player_kills": 25,
                             "minecraft:walk_one_cm": 150000, "minecraft:sprint_one_cm": 50000,
                             "minecraft:time_since_death": 20 * 3600, "minecraft:jump": 251},
        "minecraft:mined": {"minecraft:diamond_ore": 300, "minecraft:deepslate_diamond_ore": 2,
                            "minecraft:stone": 10},
        "minecraft:killed": {"minecraft:zombie": 100, "minecraft:creeper": 5}},
        "DataVersion": 4189}))
    (world / "advancements" / f"{U1}.json").write_text(json.dumps({
        "minecraft:story/mine_stone": {"criteria": {"get_stone": "2026-01-30 10:00:00 +0000"},
                                       "done": True},
        "minecraft:story/smelt_iron": {"criteria": {}, "done": False},
        "minecraft:recipes/misc/torch": {"criteria": {"x": "2026"}, "done": True},
        "DataVersion": 4189}))
    (world / "playerdata" / f"{U1}.dat").write_bytes(nbt.dumps({
        "Health": 20.0, "foodLevel": 20, "foodSaturationLevel": 5.0, "XpLevel": 27,
        "XpP": 0.5, "Pos": [390.4, 106.0, -225.7], "Dimension": "minecraft:overworld",
        "playerGameType": 0, "Rotation": [90.0, 0.0],
        "respawn": {"pos": [3378, 18, -1008], "dimension": "minecraft:overworld"}}))
    return inst


class TestNbt:
    def test_roundtrip(self):
        data = {"a": 1, "b": [1.5, 2.5], "c": {"d": "x"}, "e": []}
        assert nbt.loads(nbt.dumps(data)) == data

    def test_kaputt(self):
        with pytest.raises(nbt.NbtError):
            nbt.loads(b"\x1f\x8b kaputt")
        with pytest.raises(nbt.NbtError):
            nbt.loads(b"\x0a\x00\x00\x03\x00")


class TestHelfer:
    def test_blickrichtung(self):
        assert pl.facing(0) == "S"
        assert pl.facing(90) == "W"
        assert pl.facing(-90) == "O"
        assert pl.facing(180) == "N"
        assert pl.facing(None) is None

    def test_bett_altes_format(self):
        v = pl.vitals({"SpawnX": 1, "SpawnY": 64, "SpawnZ": -3})
        assert v["bed"] == {"pos": [1, 64, -3], "dimension": "minecraft:overworld"}
        assert pl.vitals({})["bed"] is None

    def test_live_parser(self):
        outputs = []
        for field in pl.LIVE_FIELDS:
            outputs.append({"Health": "Alex has the following entity data: 15.0f",
                            "foodLevel": "Alex has the following entity data: 17",
                            "Pos": "Alex has the following entity data: [1.2d, 64.0d, 3.9d]",
                            }.get(field, "Found no elements matching " + field))
        outputs += ["No entity was found"] * len(pl.LIVE_FIELDS)
        rows = pl.parse_live(["Alex", "Weg"], outputs)
        assert len(rows) == 1
        assert rows[0]["health"] == 15.0
        assert rows[0]["food"] == 17
        assert rows[0]["pos"] == [1, 64, 4]


class TestListe:
    def test_alle_spieler_mit_rollen(self, server):
        data = pl.list_players(server["id"], ["Steve", "Neuling"])
        by_name = {p["name"]: p for p in data["players"]}
        assert set(by_name) == {"Alex", "Steve", "Neuling"}
        assert by_name["Alex"]["op_level"] == 4
        assert by_name["Alex"]["whitelisted"] is True
        assert by_name["Alex"]["play_seconds"] == 3600
        assert by_name["Alex"]["advancements"] == 1
        assert by_name["Alex"]["player_kills"] == 25
        assert by_name["Steve"]["banned"] is True
        assert by_name["Steve"]["online"] is True
        assert by_name["Neuling"]["uuid"] is None
        # Online zuerst
        assert data["players"][0]["online"]

    def test_ohne_welt(self):
        inst = instances.create_instance("Leer", "fabric", "1.21.4", accept_eula=True)
        assert pl.list_players(inst["id"])["players"] == []


class TestProfil:
    def test_profil(self, server):
        history.update_sessions(1000, server["id"], ["Alex"], 30)
        history.update_sessions(1030, server["id"], [], 30)
        history.update_sessions(1060, server["id"], [], 30)
        history.update_sessions(5000, server["id"], ["Alex"], 30)
        p = pl.player_profile(server["id"], "alex")
        assert p["uuid"] == U1
        s = p["stats"]
        assert s["mob_kills"] == 12522
        assert s["diamonds"] == 302
        assert s["distance_m"] == 2000
        assert s["since_death_seconds"] == 3600
        assert s["top_kills"][0] == {"mob": "zombie", "count": 100}
        assert [a["id"] for a in p["advancements"]] == ["minecraft:story/mine_stone"]
        saved = p["saved"]
        assert saved["level"] == 27
        assert saved["pos"] == [390, 106, -226]
        assert saved["bed"]["pos"] == [3378, 18, -1008]
        assert saved["facing"] == "W"
        assert p["sessions"]["count"] == 2
        assert p["sessions"]["current_start"] == 5000
        assert p["sessions"]["first_seen"] == 1000
        # Auch per UUID
        assert pl.player_profile(server["id"], U1)["name"] == "Alex"

    def test_wetter(self, server):
        assert pl.world_weather(instances.world_dir(server["id"])) == "regen"


class TestRouten:
    def test_liste_gestoppt(self, client, server, monkeypatch):
        from app import runtime
        monkeypatch.setattr(runtime, "is_running", lambda inst: False)
        r = client.get(f"/api/instances/{server['id']}/known-players")
        assert r.status_code == 200
        assert r.json()["running"] is False
        assert len(r.json()["players"]) == 2

    def test_profil_online_mit_live(self, client, server, monkeypatch):
        from app import rcon as rcon_mod
        from app import runtime
        monkeypatch.setattr(runtime, "is_running", lambda inst: True)
        monkeypatch.setattr(runtime, "rcon_target", lambda inst: ("127.0.0.1", 59998))
        monkeypatch.setattr(runtime, "rcon_secret", lambda inst: "secret")
        monkeypatch.setattr(rcon_mod, "command", lambda *a, **k:
                            "There are 1 of a max of 20 players online: Alex")

        def fake_commands(host, port, password, cmds, timeout=5.0):
            out = []
            for cmd in cmds:
                if cmd.endswith(" Health"):
                    out.append("Alex has the following entity data: 12.0f")
                elif cmd == "time query daytime":
                    out.append("The time is 1000")
                else:
                    out.append("Found no elements matching")
            return out

        monkeypatch.setattr(rcon_mod, "commands", fake_commands)
        r = client.get(f"/api/instances/{server['id']}/known-players/Alex")
        assert r.status_code == 200
        assert r.json()["live"]["health"] == 12.0
        assert r.json()["online"] is True
        r = client.get(f"/api/instances/{server['id']}/players-live")
        assert r.status_code == 200
        body = r.json()
        assert body["players"][0]["name"] == "Alex"
        assert body["daytime"] == 1000
        assert body["weather"] == "regen"

    def test_ungueltiger_name(self, client, server):
        r = client.get(f"/api/instances/{server['id']}/known-players/a%20b")
        assert r.status_code == 400

    def test_live_gestoppt_409(self, client, server, monkeypatch):
        from app import runtime
        monkeypatch.setattr(runtime, "is_running", lambda inst: False)
        r = client.get(f"/api/instances/{server['id']}/players-live")
        assert r.status_code == 409
