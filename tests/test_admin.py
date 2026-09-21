"""Tests für Config-Editor (server.properties) und RCON-Spieler-Verwaltung."""
import shutil

import pytest

from app import instances, runtime
from app import rcon as rcon_mod
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen():
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    yield


@pytest.fixture()
def instanz():
    return instances.create_instance("Admin-Srv", "fabric", "1.21.4",
                                     accept_eula=True)


class TestConfigApi:
    def test_get_und_post(self, client, instanz):
        r = client.get(f"/api/instances/{instanz['id']}/config")
        assert r.status_code == 200
        props = r.json()["properties"]
        assert any(p["key"] == "motd" for p in props)

        body = {"properties": [
            p if p["key"] != "view-distance"
            else {"key": "view-distance", "value": "12"}
            for p in props]}
        r2 = client.post(f"/api/instances/{instanz['id']}/config", json=body)
        assert r2.status_code == 200
        assert r2.json()["restart_required"] is False  # Instanz läuft nicht

        reread = {p["key"]: p["value"]
                  for p in client.get(
                      f"/api/instances/{instanz['id']}/config").json()["properties"]}
        assert reread["view-distance"] == "12"

    def test_ungültige_eingaben_abgelehnt(self, client, instanz):
        # Zeilenumbruch im Wert (Pydantic-Pattern oder Writer-Prüfung)
        r = client.post(f"/api/instances/{instanz['id']}/config",
                        json={"properties": [{"key": "motd", "value": "x\ny"}]})
        assert r.status_code in (400, 422)
        # Ungültiger Schlüssel
        r = client.post(f"/api/instances/{instanz['id']}/config",
                        json={"properties": [{"key": "boese key", "value": "1"}]})
        assert r.status_code in (400, 422)
        # Datei unverändert
        assert client.get(f"/api/instances/{instanz['id']}/config").status_code == 200

    def test_unbekannte_instanz_404(self, client):
        assert client.get("/api/instances/gibtsnicht/config").status_code == 404
        assert client.post(
            "/api/instances/gibtsnicht/config",
            json={"properties": []}).status_code == 404


class TestRconApi:
    def test_gestoppt_409(self, client, instanz):
        r = client.post(f"/api/instances/{instanz['id']}/rcon",
                        json={"action": "list"})
        assert r.status_code == 409
        r = client.get(f"/api/instances/{instanz['id']}/players")
        assert r.status_code == 409

    def test_op_kommando_wird_gesendet(self, client, instanz, monkeypatch):
        monkeypatch.setattr(runtime, "is_running", lambda inst: True)
        calls = []

        def fake_command(host, port, password, cmd, timeout=5.0):
            calls.append((host, port, password, cmd))
            return "Added Steve to the ops list"

        monkeypatch.setattr(rcon_mod, "command", fake_command)
        r = client.post(f"/api/instances/{instanz['id']}/rcon",
                        json={"action": "op", "target": "Steve"})
        assert r.status_code == 200
        data = r.json()
        assert data["command"] == "op Steve"
        assert "ops list" in data["output"]
        assert calls and calls[0][3] == "op Steve"

    def test_ban_mit_grund(self, client, instanz, monkeypatch):
        monkeypatch.setattr(runtime, "is_running", lambda inst: True)
        monkeypatch.setattr(rcon_mod, "command", lambda *a, **k: "Steve is banned")
        r = client.post(f"/api/instances/{instanz['id']}/rcon",
                        json={"action": "ban", "target": "Steve", "reason": "Griefing"})
        assert r.status_code == 200
        assert r.json()["command"] == "ban Steve Griefing"

    def test_ohne_target_400(self, client, instanz, monkeypatch):
        monkeypatch.setattr(runtime, "is_running", lambda inst: True)
        r = client.post(f"/api/instances/{instanz['id']}/rcon",
                        json={"action": "op"})
        assert r.status_code == 400

    def test_unbekannte_aktion_422(self, client, instanz):
        r = client.post(f"/api/instances/{instanz['id']}/rcon",
                        json={"action": "rm -rf", "target": "Steve"})
        assert r.status_code == 422

    def test_players_parsen(self, client, instanz, monkeypatch):
        monkeypatch.setattr(runtime, "is_running", lambda inst: True)
        monkeypatch.setattr(
            rcon_mod, "command",
            lambda *a, **k: "There are 2 of a max of 20 players online: Steve, Alex")
        r = client.get(f"/api/instances/{instanz['id']}/players")
        assert r.status_code == 200
        data = r.json()
        assert data["online"] == 2
        assert data["max"] == 20
        assert data["names"] == ["Steve", "Alex"]

    def test_players_unparsbar_roh_rückgabe(self, client, instanz, monkeypatch):
        monkeypatch.setattr(runtime, "is_running", lambda inst: True)
        monkeypatch.setattr(rcon_mod, "command", lambda *a, **k: "?")
        r = client.get(f"/api/instances/{instanz['id']}/players")
        assert r.status_code == 200
        assert r.json()["names"] == []
        assert r.json()["raw"] == "?"
