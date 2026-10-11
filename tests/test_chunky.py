"""Tests für den Chunky-Tab (app/chunky.py + Routen)."""
import shutil

import pytest
from fastapi import HTTPException

from app import chunky, instances
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen():
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    yield
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


@pytest.fixture()
def instanz():
    return instances.create_instance("Chunky-Srv", "fabric", "1.21.4",
                                     accept_eula=True)


class TestErkennung:
    def test_nicht_installiert(self, instanz):
        assert chunky.find_chunky(instanz["id"])["installed"] is False

    def test_mod_und_plugin(self, instanz):
        root = instances.instance_dir(instanz["id"])
        (root / "mods").mkdir(exist_ok=True)
        (root / "mods" / "Chunky-Fabric-1.4.jar").write_bytes(b"x")
        info = chunky.find_chunky(instanz["id"])
        assert info == {"installed": True, "jar": "Chunky-Fabric-1.4.jar",
                        "kind": "mod", "disabled": False}
        (root / "mods" / "Chunky-Fabric-1.4.jar").rename(
            root / "mods" / "Chunky-Fabric-1.4.jar.disabled")
        info = chunky.find_chunky(instanz["id"])
        assert info["installed"] is False and info["disabled"] is True
        (root / "plugins").mkdir()
        (root / "plugins" / "Chunky-Bukkit.jar").write_bytes(b"x")
        assert chunky.find_chunky(instanz["id"])["kind"] == "plugin"


class TestKommandos:
    def test_start_mit_einstellungen(self):
        cmds = chunky.build_commands({
            "action": "start", "world": "minecraft:overworld", "center_x": "0",
            "center_z": -64, "radius": 2000, "shape": "circle",
            "pattern": "spiral"})
        assert cmds == ["chunky world minecraft:overworld", "chunky center 0 -64",
                        "chunky radius 2000", "chunky shape circle",
                        "chunky pattern spiral", "chunky start"]

    def test_einfache_aktionen(self):
        for action in ("pause", "continue", "cancel", "progress"):
            assert chunky.build_commands({"action": action}) == [f"chunky {action}"]

    def test_worldborder(self):
        assert chunky.build_commands({"action": "worldborder", "world": "world"}) == [
            "chunky world world", "chunky worldborder"]

    @pytest.mark.parametrize("req", [
        {"action": "rm"},
        {"action": "apply"},
        {"action": "start", "world": "a; stop"},
        {"action": "start", "radius": 0},
        {"action": "start", "radius": "abc"},
        {"action": "start", "center_x": 5},
        {"action": "start", "shape": "blob"},
        {"action": "start", "pattern": "x\nstop"},
    ])
    def test_ungueltig(self, req):
        with pytest.raises(HTTPException) as e:
            chunky.build_commands(req)
        assert e.value.status_code == 400


class TestFortschritt:
    def test_laufend(self):
        out = ("§e[Chunky] Task running for minecraft:overworld. Processed: "
               "1,234 chunks (12.34%), ETA: 0:12:34, Rate: 123.4 cps, Current: 1, -2")
        p = chunky.parse_progress(out)
        assert p["state"] == "running" and p["world"] == "minecraft:overworld"
        assert p["chunks"] == 1234 and p["percent"] == 12.34
        assert p["eta"] == "0:12:34" and p["rate"] == 123.4
        assert p["current"] == [1, -2]

    def test_leerlauf_und_pause(self):
        assert chunky.parse_progress("[Chunky] No tasks running.")["state"] == "idle"
        assert chunky.parse_progress(
            "Task paused for world. Processed: 5 chunks (1.00%)")["state"] == "paused"
        assert chunky.parse_progress("")["state"] == "unknown"


class TestRouten:
    def _fake(self, monkeypatch, running=True):
        from app import rcon as rcon_mod
        from app import runtime
        monkeypatch.setattr(runtime, "is_running", lambda inst: running)
        monkeypatch.setattr(runtime, "rcon_target", lambda inst: ("127.0.0.1", 59998))
        monkeypatch.setattr(runtime, "rcon_secret", lambda inst: "secret")
        sent = []

        def fake_command(host, port, password, cmd, timeout=5.0):
            sent.append([cmd])
            return ("Task running for minecraft:overworld. Processed: 10 chunks "
                    "(50.00%), ETA: 0:00:10, Rate: 1.0 cps, Current: 0, 0")

        def fake_commands(host, port, password, cmds, timeout=5.0):
            sent.append(list(cmds))
            return ["ok"] * (len(cmds) - 1) + [
                "No tasks running."]
        monkeypatch.setattr(rcon_mod, "command", fake_command)
        monkeypatch.setattr(rcon_mod, "commands", fake_commands)
        return sent

    def _install(self, instanz):
        root = instances.instance_dir(instanz["id"])
        (root / "mods").mkdir(exist_ok=True)
        (root / "mods" / "chunky.jar").write_bytes(b"x")

    def test_get_ohne_chunky(self, client, instanz, monkeypatch):
        self._fake(monkeypatch)
        r = client.get(f"/api/instances/{instanz['id']}/chunky")
        assert r.status_code == 200
        assert r.json()["installed"] is False and r.json()["progress"] is None

    def test_get_mit_fortschritt(self, client, instanz, monkeypatch):
        sent = self._fake(monkeypatch)
        self._install(instanz)
        data = client.get(f"/api/instances/{instanz['id']}/chunky").json()
        assert data["installed"] and data["running"]
        assert data["progress"]["percent"] == 50.0
        assert sent == [["chunky progress"]]

    def test_get_gestoppt_kein_rcon(self, client, instanz, monkeypatch):
        sent = self._fake(monkeypatch, running=False)
        self._install(instanz)
        data = client.get(f"/api/instances/{instanz['id']}/chunky").json()
        assert data["running"] is False and data["progress"] is None
        assert sent == []

    def test_start(self, client, instanz, monkeypatch):
        sent = self._fake(monkeypatch)
        self._install(instanz)
        r = client.post(f"/api/instances/{instanz['id']}/chunky",
                        json={"action": "start", "radius": 500, "shape": "square"})
        assert r.status_code == 200
        assert sent[-1] == ["chunky radius 500", "chunky shape square",
                            "chunky start", "chunky progress"]
        assert r.json()["progress"]["state"] == "idle"

    def test_fehler(self, client, instanz, monkeypatch):
        self._fake(monkeypatch)
        url = f"/api/instances/{instanz['id']}/chunky"
        assert client.post(url, json={"action": "start"}).status_code == 409  # nicht installiert
        self._install(instanz)
        assert client.post(url, json={"action": "start",
                                      "radius": -3}).status_code == 400
        assert client.post(url, json={"action": "boom"}).status_code == 422
        self._fake(monkeypatch, running=False)
        assert client.post(url, json={"action": "pause"}).status_code == 409

    def test_viewer_darf_nicht_steuern(self, client, instanz, monkeypatch):
        self._fake(monkeypatch)
        self._install(instanz)
        client.post("/api/auth/setup", json={"username": "boss",
                                             "password": "passwort123"})
        client.post("/api/auth/users", json={"username": "leser",
                                             "password": "passwort123",
                                             "role": "viewer"})
        client.cookies.clear()
        client.post("/api/auth/login", json={"username": "leser",
                                             "password": "passwort123"})
        url = f"/api/instances/{instanz['id']}/chunky"
        assert client.get(url).status_code == 200
        assert client.post(url, json={"action": "pause"}).status_code == 403
