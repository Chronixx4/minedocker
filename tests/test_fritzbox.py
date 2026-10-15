"""Tests für die Fritzbox-Anbindung (TR-064): Konfiguration, Portlisten, Aufrufe."""
import pytest

from app import fritzbox


@pytest.fixture(autouse=True)
def _sauber():
    fritzbox.config_path().unlink(missing_ok=True)
    yield
    fritzbox.config_path().unlink(missing_ok=True)


def _setup(auto=False):
    fritzbox.save(host="192.168.178.1", user="mc", password="geheim",
                  lan_ip="192.168.178.20", auto=auto)


class TestConfig:
    def test_passwort_wird_nie_ausgegeben(self):
        _setup()
        view = fritzbox.public_view()
        assert view["has_password"] is True
        assert "password" not in view
        assert "geheim" not in str(view)

    def test_leeres_passwort_laesst_altes_stehen(self):
        _setup()
        fritzbox.save(password="", user="neu")
        cfg = fritzbox.load()
        assert cfg["password"] == "geheim" and cfg["user"] == "neu"

    def test_datei_nur_fuer_besitzer(self):
        _setup()
        assert fritzbox.config_path().stat().st_mode & 0o077 == 0

    @pytest.mark.parametrize("ip", ["8.8.8.8", "abc", "::1", "192.168.178"])
    def test_lan_ip_validiert(self, ip):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            fritzbox.save(lan_ip=ip)

    def test_host_validiert(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            fritzbox.save(host="evil/../x")


class TestPorts:
    def test_required_ports(self):
        inst = {"id": "a", "name": "A", "port": 25570,
                "extra_ports": [{"port": 24454, "proto": "udp", "label": "Voice"}],
                "map": {"enabled": True, "port": 8100, "public": True}}
        ports = {(p["port"], p["proto"]) for p in fritzbox.required_ports(inst)}
        assert ports == {(25570, "tcp"), (24454, "udp"), (8100, "tcp")}

    def test_karte_nur_wenn_oeffentlich(self):
        inst = {"id": "a", "name": "A", "port": 25570,
                "map": {"enabled": True, "port": 8100}}
        assert [p["port"] for p in fritzbox.required_ports(inst)] == [25570]


class TestCalls:
    def test_open_und_close(self, monkeypatch):
        _setup()
        calls = []
        table = [{"NewExternalPort": "25570", "NewProtocol": "TCP", "NewInternalPort": "25570",
                  "NewInternalClient": "192.168.178.20", "NewEnabled": "1",
                  "NewPortMappingDescription": "minedocker: A Spiel"},
                 {"NewExternalPort": "80", "NewProtocol": "TCP", "NewInternalPort": "80",
                  "NewInternalClient": "192.168.178.5", "NewEnabled": "1",
                  "NewPortMappingDescription": "fremd"}]

        def fake(cfg, action, args=None):
            calls.append((action, args))
            if action == "GetGenericPortMappingEntry":
                i = args["NewPortMappingIndex"]
                if i >= len(table):
                    raise fritzbox.FritzError("Fritzbox-Fehler 713: SpecifiedArrayIndexInvalid")
                return table[i]
            return {}

        monkeypatch.setattr(fritzbox, "_call", fake)
        inst = {"id": "a", "name": "A", "port": 25570}
        fritzbox.open_ports(inst)
        add = next(a for n, a in calls if n == "AddPortMapping")
        assert add["NewInternalClient"] == "192.168.178.20"
        assert add["NewProtocol"] == "TCP" and add["NewLeaseDuration"] == 0
        fritzbox.close_ports(inst)
        assert next(a for n, a in calls if n == "DeletePortMapping")["NewExternalPort"] == 25570

    def test_fremde_freigabe_bleibt(self, monkeypatch):
        _setup()
        deleted = []

        def fake(cfg, action, args=None):
            if action == "GetGenericPortMappingEntry":
                if args["NewPortMappingIndex"] == 0:
                    return {"NewExternalPort": "25570", "NewProtocol": "TCP",
                            "NewInternalPort": "25570", "NewInternalClient": "x",
                            "NewEnabled": "1", "NewPortMappingDescription": "manuell"}
                raise fritzbox.FritzError("713")
            deleted.append(action)
            return {}

        monkeypatch.setattr(fritzbox, "_call", fake)
        fritzbox.close_ports({"id": "a", "name": "A", "port": 25570})
        assert deleted == []

    def test_auto_sync_aus_ruft_nichts(self, monkeypatch):
        _setup(auto=False)
        monkeypatch.setattr(fritzbox, "_call", lambda *a, **k: pytest.fail("kein Aufruf erwartet"))
        fritzbox.auto_sync({"id": "a", "port": 25570}, opening=True)

    def test_auto_sync_schluckt_fehler(self, monkeypatch):
        _setup(auto=True)

        def boom(*a, **k):
            raise fritzbox.FritzError("weg")

        monkeypatch.setattr(fritzbox, "_call", boom)
        fritzbox.auto_sync({"id": "a", "name": "A", "port": 25570}, opening=True)

    def test_nicht_eingerichtet_409(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            fritzbox.open_ports({"id": "a", "name": "A", "port": 25570})
        assert exc.value.status_code == 409


class TestApi:
    def test_config_roundtrip_ohne_passwort(self, client):
        r = client.put("/api/fritzbox", json={"user": "mc", "password": "pw123456",
                                              "lan_ip": "192.168.178.20", "auto": True})
        assert r.status_code == 200
        got = client.get("/api/fritzbox").json()
        assert got["has_password"] and got["auto"] and "password" not in got

    def test_ungueltige_lan_ip_400(self, client):
        assert client.put("/api/fritzbox", json={"lan_ip": "1.2.3.4"}).status_code == 400


class TestDienstWahl:
    def test_ungueltige_aktion_probiert_naechsten_dienst(self, monkeypatch):
        import httpx

        _setup()
        fritzbox._working.clear()
        monkeypatch.setattr(fritzbox, "_discover", lambda cfg: [
            ("/upnp/control/a", "urn:x:WANIPConnection:1"),
            ("/upnp/control/b", "urn:x:WANPPPConnection:1")])
        fault = ("<s:Envelope xmlns:s='x'><s:Body><s:Fault><detail><UPnPError>"
                 "<errorCode>401</errorCode><errorDescription>Invalid Action"
                 "</errorDescription></UPnPError></detail></s:Fault></s:Body></s:Envelope>")
        ok = ("<s:Envelope xmlns:s='x'><s:Body><u:R xmlns:u='y'>"
              "<NewExternalIPAddress>1.2.3.4</NewExternalIPAddress></u:R></s:Body></s:Envelope>")

        def fake_post(url, **kw):
            if url.endswith("/a"):
                return httpx.Response(500, text=fault)
            return httpx.Response(200, text=ok)

        monkeypatch.setattr(httpx, "post", fake_post)
        out = fritzbox._call(fritzbox.load(), "GetExternalIPAddress")
        assert out["NewExternalIPAddress"] == "1.2.3.4"
        assert fritzbox._working["192.168.178.1"][0].endswith("/b")
