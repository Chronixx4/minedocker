"""Tests für die freie RCON-Konsole und die Whitelist-Verwaltung
(whitelist.json offline editieren, UUID-Auflösung, RCON-Reload)."""
import json
import shutil
import uuid as uuid_mod

import pytest

from app import instances, runtime
from app import rcon as rcon_mod
from app import whitelist as wl
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
    return instances.create_instance("Konsole-Srv", "fabric", "1.21.4",
                                     accept_eula=True)


# ---------------------------------------------------------------------------
# Konsole (freie RCON-Befehle)
# ---------------------------------------------------------------------------

class TestKonsole:
    def test_gestoppt_409(self, client, instanz):
        r = client.post(f"/api/instances/{instanz['id']}/console",
                        json={"command": "say hallo"})
        assert r.status_code == 409

    def test_whitelist_modus_erlaubte_befehle(self, client, instanz, monkeypatch):
        monkeypatch.setattr(runtime, "is_running", lambda inst: True)
        calls = []
        monkeypatch.setattr(rcon_mod, "command",
                            lambda h, p, pw, cmd, timeout=5.0: calls.append(cmd)
                            or "wird gesendet")
        for cmd in ("say hallo", "op Steve", "whitelist add Alex",
                    "banlist players", "/say mit-slash"):
            calls.clear()
            r = client.post(f"/api/instances/{instanz['id']}/console",
                            json={"command": cmd})
            assert r.status_code == 200, cmd
            assert calls == [cmd.lstrip("/")], cmd

    def test_whitelist_modus_blockt_stop(self, client, instanz, monkeypatch):
        monkeypatch.setattr(runtime, "is_running", lambda inst: True)
        sent = []
        monkeypatch.setattr(rcon_mod, "command",
                            lambda *a, **k: sent.append(a[3]) or "")
        r = client.post(f"/api/instances/{instanz['id']}/console",
                        json={"command": "stop"})
        assert r.status_code == 403
        assert not sent
        r = client.post(f"/api/instances/{instanz['id']}/console",
                        json={"command": "save-off"})
        assert r.status_code == 403

    def test_freier_modus_zeigt_und_erlaubt_alles(self, client, instanz, monkeypatch):
        monkeypatch.setattr(settings, "rcon_console_mode", "free")
        monkeypatch.setattr(runtime, "is_running", lambda inst: True)
        calls = []
        monkeypatch.setattr(rcon_mod, "command",
                            lambda h, p, pw, cmd, timeout=5.0: calls.append(cmd) or "ok")
        r = client.post(f"/api/instances/{instanz['id']}/console",
                        json={"command": "stop"})
        assert r.status_code == 200
        assert calls == ["stop"]
        r = client.get("/api/settings")
        assert r.json()["rcon_console_mode"] == "free"

    def test_zusatz_whitelist_env(self, client, instanz, monkeypatch):
        monkeypatch.setattr(settings, "rcon_console_whitelist", ["save-all"])
        monkeypatch.setattr(runtime, "is_running", lambda inst: True)
        calls = []
        monkeypatch.setattr(rcon_mod, "command",
                            lambda h, p, pw, cmd, timeout=5.0: calls.append(cmd) or "ok")
        r = client.post(f"/api/instances/{instanz['id']}/console",
                        json={"command": "save-all flush"})
        assert r.status_code == 200
        assert calls == ["save-all flush"]
        # Settings listet den Zusatzbefehl mit auf
        listed = client.get("/api/settings").json()["rcon_console_whitelist"]
        assert "save-all" in listed

    def test_leerer_und_gesteuerter_befehl(self, client, instanz, monkeypatch):
        monkeypatch.setattr(runtime, "is_running", lambda inst: True)
        monkeypatch.setattr(rcon_mod, "command", lambda *a, **k: "ok")
        r = client.post(f"/api/instances/{instanz['id']}/console",
                        json={"command": "   "})
        assert r.status_code == 400
        # Steuerzeichen (Zeilenumbruch) werden entfernt statt weitergereicht
        r = client.post(f"/api/instances/{instanz['id']}/console",
                        json={"command": "say hi\nrm -rf"})
        assert r.status_code == 200
        assert r.json()["command"] == "say hirm -rf"

    def test_unbekannte_instanz_404(self, client):
        assert client.post("/api/instances/gibtsnicht/console",
                           json={"command": "list"}).status_code == 404

    def test_settings_listen_modus(self, client):
        r = client.get("/api/settings")
        assert r.json()["rcon_console_mode"] == "whitelist"
        assert "op" in r.json()["rcon_console_whitelist"]


# ---------------------------------------------------------------------------
# Whitelist: Datei-Handling
# ---------------------------------------------------------------------------

class TestWhitelistDatei:
    def test_offline_uuid_entspricht_java_algorithmus(self):
        # Format: Version-3-UUID (MD5), IETF-Variante, deterministisch
        u = wl._offline_uuid("Steve")
        parsed = uuid_mod.UUID(u)
        assert parsed.version == 3
        assert (parsed.bytes[8] & 0xC0) == 0x80
        assert wl._offline_uuid("Steve") == u  # deterministisch
        assert wl._offline_uuid("steve") != u  # case-sensitiv wie Java

    def test_dashed_uuid(self):
        assert wl._dashed_uuid("069a79f444e94726a5befca90e38abaf") == \
            "069a79f4-44e9-4726-a5be-fca90e38abaf"
        assert wl._dashed_uuid("069A79F444E94726A5BEFCA90E38ABAF") == \
            "069a79f4-44e9-4726-a5be-fca90e38abaf"
        assert wl._dashed_uuid("kurz") is None
        assert wl._dashed_uuid(None) is None

    def test_read_ohne_datei(self, instanz):
        data = wl.read_whitelist(instanz["id"])
        assert data == {"entries": [], "exists": False, "corrupt": False}

    def test_read_toleriert_defekte_datei(self, instanz):
        d = instances.instance_dir(instanz["id"])
        (d / "whitelist.json").write_text("{kaputt", encoding="utf-8")
        data = wl.read_whitelist(instanz["id"])
        assert data["exists"] is True and data["corrupt"] is True
        assert data["entries"] == []

    def test_read_überspringt_ungültige_einträge(self, instanz):
        d = instances.instance_dir(instanz["id"])
        (d / "whitelist.json").write_text(json.dumps([
            {"uuid": "069a79f4-44e9-4726-a5be-fca90e38abaf", "name": "Steve"},
            {"name": ""},
            "kaputt",
            {"uuid": "x", "name": "Alex"},
        ]), encoding="utf-8")
        entries = wl.read_whitelist(instanz["id"])["entries"]
        assert entries == [
            {"uuid": "069a79f4-44e9-4726-a5be-fca90e38abaf", "name": "Steve"},
            {"name": "Alex"},
        ]

    def test_online_mode_aus_props(self, instanz):
        assert wl.read_online_mode(instanz["id"]) is True  # Default
        d = instances.instance_dir(instanz["id"])
        (d / "server.properties").write_text("online-mode=false\n",
                                             encoding="utf-8")
        assert wl.read_online_mode(instanz["id"]) is False


# ---------------------------------------------------------------------------
# Whitelist: Schreiben + API
# ---------------------------------------------------------------------------

def _patch_mojang(monkeypatch, mapping):
    """Mojang-Lookup ersetzen: name → dashed UUID oder None."""

    async def fake_lookup(name):
        return mapping.get(name)

    monkeypatch.setattr(wl, "_lookup_uuid_online", fake_lookup)


class TestWhitelistApi:
    def test_get_leer(self, client, instanz):
        r = client.get(f"/api/instances/{instanz['id']}/whitelist")
        assert r.status_code == 200
        body = r.json()
        assert body["entries"] == [] and body["exists"] is False
        assert body["online_mode"] is True and body["running"] is False

    def test_schreiben_online_mode_mit_mojang(self, client, instanz, monkeypatch):
        _patch_mojang(monkeypatch, {"Steve": "069a79f444e94726a5befca90e38abaf"})
        r = client.post(f"/api/instances/{instanz['id']}/whitelist",
                        json={"entries": ["Steve", " Alex ", "steve", "Mystery"]})
        assert r.status_code == 200
        body = r.json()
        names = [e["name"] for e in body["entries"]]
        assert names == ["Steve", "Alex", "Mystery"]  # dedupe case-insensitive
        assert body["entries"][0]["uuid"] == \
            "069a79f4-44e9-4726-a5be-fca90e38abaf"
        assert body["unresolved"] == ["Alex", "Mystery"]
        assert body["reloaded"] is None  # läuft nicht
        # Datei tatsächlich geschrieben
        path = instances.instance_dir(instanz["id"]) / "whitelist.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert [e["name"] for e in raw] == ["Steve", "Alex", "Mystery"]
        assert set(raw[2]) == {"name"}  # ohne uuid

    def test_schreiben_offline_mode_berechnet_uuid(self, client, instanz):
        d = instances.instance_dir(instanz["id"])
        (d / "server.properties").write_text("online-mode=false\n",
                                             encoding="utf-8")
        r = client.post(f"/api/instances/{instanz['id']}/whitelist",
                        json={"entries": ["Steve"]})
        assert r.status_code == 200
        entry = r.json()["entries"][0]
        assert entry["uuid"] == wl._offline_uuid("Steve")
        raw = json.loads((d / "whitelist.json").read_text(encoding="utf-8"))
        assert raw == [entry]

    def test_ungültiger_name_400(self, client, instanz):
        r = client.post(f"/api/instances/{instanz['id']}/whitelist",
                        json={"entries": ["boeser;name"]})
        assert r.status_code == 400
        assert not (instances.instance_dir(instanz["id"]) / "whitelist.json").exists()

    def test_leere_liste_räumt_auf(self, client, instanz):
        d = instances.instance_dir(instanz["id"])
        (d / "whitelist.json").write_text(
            json.dumps([{"name": "Alt"}]), encoding="utf-8")
        r = client.post(f"/api/instances/{instanz['id']}/whitelist",
                        json={"entries": []})
        assert r.status_code == 200
        assert json.loads((d / "whitelist.json").read_text()) == []

    def test_speichern_bei_laufendem_server_reloaded(self, client, instanz,
                                                     monkeypatch):
        _patch_mojang(monkeypatch, {"Steve": "069a79f444e94726a5befca90e38abaf"})
        monkeypatch.setattr(runtime, "is_running", lambda inst: True)
        sent = []
        monkeypatch.setattr(rcon_mod, "command",
                            lambda h, p, pw, cmd, timeout=5.0:
                            sent.append(cmd) or "Whitelist neu geladen")
        r = client.post(f"/api/instances/{instanz['id']}/whitelist",
                        json={"entries": ["Steve"]})
        assert r.status_code == 200
        assert r.json()["reloaded"] is True
        assert sent == ["whitelist reload"]

    def test_reload_endpunkt(self, client, instanz, monkeypatch):
        # Gestoppt → 409
        r = client.post(f"/api/instances/{instanz['id']}/whitelist/reload")
        assert r.status_code == 409
        # Laufend, RCON ok → 200
        monkeypatch.setattr(runtime, "is_running", lambda inst: True)
        monkeypatch.setattr(rcon_mod, "command",
                            lambda h, p, pw, cmd, timeout=5.0: "neu geladen")
        assert client.post(
            f"/api/instances/{instanz['id']}/whitelist/reload").status_code == 200
        # Laufend, RCON kaputt → 503
        def boom(*a, **k):
            raise rcon_mod.RconError("connection refused")
        monkeypatch.setattr(rcon_mod, "command", boom)
        r = client.post(f"/api/instances/{instanz['id']}/whitelist/reload")
        assert r.status_code == 503

    def test_unbekannte_instanz_404(self, client):
        assert client.get("/api/instances/gibtsnicht/whitelist").status_code == 404
        assert client.post("/api/instances/gibtsnicht/whitelist",
                           json={"entries": []}).status_code == 404

    def test_zu_viele_einträge_400(self, client, instanz):
        names = [f"Spieler{i:03d}" for i in range(201)]
        r = client.post(f"/api/instances/{instanz['id']}/whitelist",
                        json={"entries": names})
        # Modell-Limit (Pydantic) oder Modul-Limit greifen
        assert r.status_code in (400, 422)
