"""Tests für den Gamerule-Quick-Editor (app/gamerules.py + Routen)."""
import shutil

import pytest
from fastapi import HTTPException

from app import gamerules as gr
from app import instances
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
    return instances.create_instance("Gamerule-Srv", "fabric", "1.21.4",
                                     accept_eula=True)


class TestWhitelist:
    def test_kuratierte_liste_plausibel(self):
        names = {g["name"] for g in gr.GAMERULES}
        assert {"keepInventory", "doDaylightCycle", "mobGriefing", "doFireTick",
                "doMobSpawning", "randomTickSpeed", "maxCommandChainLength",
                "announceAdvancements", "naturalRegeneration"} <= names
        # Bedrock-only Regeln dürfen nicht drin sein
        assert "pvp" not in names
        assert "showCoordinates" not in names
        for g in gr.GAMERULES:
            assert g["type"] in ("bool", "int")
            if g["type"] == "int":
                assert g["min"] is not None and g["max"] is not None
                assert g["min"] <= g["default"] <= g["max"]

    def test_validate_bool(self):
        entry = gr.known("keepInventory")
        assert gr.validate_value(entry, True) == "true"
        assert gr.validate_value(entry, "FALSE") == "false"
        with pytest.raises(HTTPException) as e:
            gr.validate_value(entry, "ja")
        assert e.value.status_code == 400

    def test_validate_int_range(self):
        entry = gr.known("randomTickSpeed")
        assert gr.validate_value(entry, "3") == "3"
        assert gr.validate_value(entry, 0) == "0"
        with pytest.raises(HTTPException) as e:
            gr.validate_value(entry, -1)
        assert e.value.status_code == 400
        entry = gr.known("snowAccumulationHeight")
        with pytest.raises(HTTPException):
            gr.validate_value(entry, 99)
        with pytest.raises(HTTPException) as e:
            gr.validate_value(entry, "x")
        assert e.value.status_code == 400

    def test_unbekannte_gamerule(self):
        assert gr.known("gibtsnicht") is None


class TestParser:
    def test_listen_format(self):
        output = ("gamerule announceAdvancements = true\n"
                  "gamerule commandBlockOutput = true\n"
                  "gamerule keepInventory = false\n"
                  "gamerule randomTickSpeed = 3\n"
                  "unbekannte zeile\n")
        values = gr.parse_gamerules_output(output)
        assert values == {"announceAdvancements": "true",
                          "commandBlockOutput": "true",
                          "keepInventory": "false",
                          "randomTickSpeed": "3"}

    def test_abfrage_format(self):
        output = "Gamerule keepInventory is currently set to: true"
        assert gr.parse_gamerules_output(output) == {"keepInventory": "true"}

    def test_leer_und_unbekannte_namen_ignoriert(self):
        assert gr.parse_gamerules_output("") == {}
        output = "gamerule pvp = true\ngamerule showCoordinates = true"
        assert gr.parse_gamerules_output(output) == {}


class TestTypisierung:
    def test_bool_und_int(self):
        entry = gr.known("keepInventory")
        assert gr._typed_value(entry, "true") is True
        assert gr._typed_value(entry, "false") is False
        assert gr._typed_value(entry, "vielleicht") is None
        entry = gr.known("randomTickSpeed")
        assert gr._typed_value(entry, "3") == 3
        assert gr._typed_value(entry, "x") is None
        assert gr._typed_value(entry, None) is None


class TestRouten:
    def test_get_nur_laufend_409(self, client, instanz, monkeypatch):
        # Fake-Docker: Container existiert, läuft nicht → is_running False
        from app import runtime
        monkeypatch.setattr(runtime, "is_running", lambda inst: False)
        r = client.get(f"/api/instances/{instanz['id']}/gamerules")
        assert r.status_code == 409

    def test_get_und_set_ueber_rcon(self, client, instanz, monkeypatch):
        from app import runtime
        monkeypatch.setattr(runtime, "is_running", lambda inst: True)
        # RCON-Ziel auf Test-Fake lenken
        monkeypatch.setattr(runtime, "rcon_target", lambda inst: ("127.0.0.1", 59998))
        monkeypatch.setattr(runtime, "rcon_secret", lambda inst: "secret")
        base = f"/api/instances/{instanz['id']}/gamerules"

        captured = []

        def fake_command(host, port, password, cmd, timeout=15.0):
            captured.append(cmd)
            if cmd.strip() == "gamerule":
                return ("gamerule announceAdvancements = true\n"
                        "gamerule keepInventory = true\n"
                        "gamerule randomTickSpeed = 3\n")
            return f"Gamerule {cmd.split()[1]} is currently set to: false"

        from app import rcon as rcon_mod
        monkeypatch.setattr(rcon_mod, "command", fake_command)
        r = client.get(base)
        assert r.status_code == 200
        assert captured == ["gamerule"]
        rules = {g["name"]: g for g in r.json()["gamerules"]}
        assert len(rules) == len(gr.GAMERULES)
        assert rules["keepInventory"]["value"] is True
        assert rules["doDaylightCycle"]["value"] is None  # nicht gemeldet
        assert rules["doDaylightCycle"]["default"] is True
        # Setzen (bool)
        r = client.post(base, json={"name": "doDaylightCycle", "value": False})
        assert r.status_code == 200
        assert r.json()["name"] == "doDaylightCycle"
        assert r.json()["value"] is False
        assert captured[-1] == "gamerule doDaylightCycle false"
        # Setzen (int in Range)
        r = client.post(base, json={"name": "randomTickSpeed", "value": 10})
        assert r.status_code == 200
        assert captured[-1] == "gamerule randomTickSpeed 10"
        # Unbekannte Gamerule → 400
        r = client.post(base, json={"name": "pvp", "value": True})
        assert r.status_code == 400
        # Bool-Feld mit Text → 400
        r = client.post(base, json={"name": "keepInventory", "value": "ja"})
        assert r.status_code == 400
        # int außerhalb → 400
        r = client.post(base, json={"name": "snowAccumulationHeight", "value": 99})
        assert r.status_code == 400

    def test_viewer_nur_lesen(self, client, instanz, monkeypatch):
        from app import runtime
        client.post("/api/auth/setup", json={"username": "boss",
                                             "password": "passwort123"})
        client.post("/api/auth/users", json={"username": "leser",
                                             "password": "passwort123",
                                             "role": "viewer"})
        client.cookies.clear()
        client.post("/api/auth/login", json={"username": "leser",
                                             "password": "passwort123"})
        monkeypatch.setattr(runtime, "is_running", lambda inst: False)
        # GET → 409 (gestoppt), also Guards bis zur Logik durchgelassen
        assert client.get(
            f"/api/instances/{instanz['id']}/gamerules").status_code == 409
        # POST → Guard blockt vorher mit 403
        r = client.post(f"/api/instances/{instanz['id']}/gamerules",
                        json={"name": "keepInventory", "value": True})
        assert r.status_code == 403
