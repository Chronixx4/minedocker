"""Tests für die Multi-Server-Instanz-Verwaltung (app.instances) + API-Routen."""
import json
import shutil

import pytest
from conftest import FakeContainer
from fastapi import HTTPException

from app import instances, runtime
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen():
    """Saubere Instanz-Umgebung pro Test."""
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    yield


def _create(name="Test-Server", **kwargs):
    kwargs.setdefault("loader", "fabric")
    kwargs.setdefault("game_version", "1.21.4")
    kwargs.setdefault("accept_eula", True)
    return instances.create_instance(name, **kwargs)


class TestCreateInstance:
    def test_erstellung_mit_getrennten_verzeichnissen(self):
        inst = _create()
        d = instances.instance_dir(inst["id"])
        assert (d / "instance.json").is_file()
        assert (d / "eula.txt").read_text(encoding="utf-8") == "eula=true\n"
        props = (d / "server.properties").read_text(encoding="utf-8")
        assert "motd=Test-Server" in props
        assert (d / "mods").is_dir()
        assert (d / "packs").is_dir()
        meta = json.loads((d / "instance.json").read_text(encoding="utf-8"))
        assert meta["name"] == "Test-Server"
        assert meta["loader"] == "fabric"
        assert meta["status"] == "stopped"
        assert meta["modpack"] is None

    def test_eula_pflicht(self):
        with pytest.raises(HTTPException) as e:
            instances.create_instance("OhneEula", "fabric", "1.21.4")
        assert e.value.status_code == 400

    def test_name_duplikat_409(self):
        _create(name="Dup")
        with pytest.raises(HTTPException) as e:
            _create(name="dup")  # Groß-/Kleinschreibung egal
        assert e.value.status_code == 409

    def test_loader_whitelist(self):
        with pytest.raises(HTTPException) as e:
            _create(name="BadLoader", loader="optifine")
        assert e.value.status_code == 400

    def test_ram_format(self):
        with pytest.raises(HTTPException) as e:
            _create(name="RamFalsch", memory="2GB")
        assert e.value.status_code == 400

    def test_ram_null_ungueltig(self):
        """0G/08G ist kein sinnvolles RAM-Limit → abgelehnt."""
        with pytest.raises(HTTPException) as e:
            _create(name="RamNull", memory="0G")
        assert e.value.status_code == 400
        with pytest.raises(HTTPException):
            _create(name="RamNull2", memory="08G")

    def test_get_unbekannt_404(self):
        with pytest.raises(HTTPException) as e:
            instances.get_instance("gibtsnicht")
        assert e.value.status_code == 404

    def test_pfadmanipulation_400(self):
        with pytest.raises(HTTPException) as e:
            instances.instance_dir("../evil")
        assert e.value.status_code == 400


class TestPorts:
    def test_explicit_port_kollision_409(self):
        _create(name="PortA", port=25599)
        with pytest.raises(HTTPException) as e:
            _create(name="PortB", port=25599)
        assert e.value.status_code == 409

    def test_auto_vergabe_eindeutig(self):
        created = [_create(name=f"Auto{i}") for i in range(3)]
        ports = {i["port"] for i in created}
        assert len(ports) == 3
        assert min(ports) >= settings.instances_port_base

    def test_rcon_port_gespeichert(self):
        inst = _create(name="RconPort", port=25588)
        assert inst["rcon_port"] == 25588 + 1000

    def test_rcon_port_kollision_409(self):
        # RCON-Port (Spiel-Port + 1000) zählt als belegt
        _create(name="RconA", port=25600)  # RCON: 26600
        with pytest.raises(HTTPException) as e:
            _create(name="RconB", port=26600)
        assert e.value.status_code == 409
        # umgekehrt: Spiel-Port 25600 blockiert Kandidat 24600 (dessen RCON = 25600)
        with pytest.raises(HTTPException) as e:
            _create(name="RconC", port=24600)
        assert e.value.status_code == 409


class TestServerProperties:
    def test_rundreise(self):
        inst = _create(name="Props")
        props = instances.read_server_properties(inst["id"])
        keys = {p["key"] for p in props}
        assert "server-port" in keys and "motd" in keys

        props = [p if p["key"] != "max-players" else {"key": "max-players", "value": "7"}
                 for p in props]
        count = instances.write_server_properties(inst["id"], props)
        assert count == len(props)
        reread = {p["key"]: p["value"] for p in instances.read_server_properties(inst["id"])}
        assert reread["max-players"] == "7"
        assert reread["server-port"] == "25565"  # Template-Wert (Container-intern)

    def test_ungültiger_key_400(self):
        inst = _create(name="BadKey")
        with pytest.raises(HTTPException) as e:
            instances.write_server_properties(inst["id"], [{"key": "boese key", "value": "1"}])
        assert e.value.status_code == 400

    def test_zeilenumbruch_im_wert_400(self):
        inst = _create(name="BadValue")
        with pytest.raises(HTTPException) as e:
            instances.write_server_properties(inst["id"],
                                              [{"key": "motd", "value": "x\ny"}])
        assert e.value.status_code == 400

    def test_unbekannte_instanz_404(self):
        with pytest.raises(HTTPException) as e:
            instances.read_server_properties("gibtsnicht")
        assert e.value.status_code == 404


class TestStatusUndListe:
    def test_set_status_rundreise(self):
        inst = _create(name="StatusSrv")
        instances.set_status(inst["id"], "running")
        assert instances.get_instance(inst["id"])["status"] == "running"
        instances.set_status(inst["id"], "error", "boom")
        meta = instances.get_instance(inst["id"])
        assert meta["status"] == "error"
        assert meta["error"] == "boom"

    def test_unbekannter_status_valueerror(self):
        inst = _create(name="StatusBad")
        with pytest.raises(ValueError):
            instances.set_status(inst["id"], "fliegend")

    def test_liste_enthält_alle(self):
        a = _create(name="AAA")
        b = _create(name="BBB")
        ids = [i["id"] for i in instances.list_instances()]
        assert a["id"] in ids and b["id"] in ids

    def test_beschädigte_instanz_wird_übersprungen(self):
        inst = _create(name="Gut")
        broken = settings.instances_dir / "kaputt"
        broken.mkdir()
        (broken / "instance.json").write_text("{kaputt", encoding="utf-8")
        listing = instances.list_instances()
        assert [i["id"] for i in listing] == [inst["id"]]


class TestDeleteInstance:
    def test_loeschen_gestoppt(self, fake_docker):
        inst = _create(name="DelMe")
        result = instances.delete_instance(inst["id"])
        assert result["deleted"] == inst["id"]
        assert not instances.instance_dir(inst["id"]).exists()
        with pytest.raises(HTTPException) as e:
            instances.get_instance(inst["id"])
        assert e.value.status_code == 404

    def test_loeschen_laufend_409_dann_force(self, fake_docker):
        inst = _create(name="Running")
        cname = runtime.container_name(inst["id"])
        fake_docker.containers._items[cname] = FakeContainer(cname, running=True)
        with pytest.raises(HTTPException) as e:
            instances.delete_instance(inst["id"])
        assert e.value.status_code == 409
        result = instances.delete_instance(inst["id"], force=True)
        assert result["deleted"] == inst["id"]
        assert fake_docker.containers._items[cname].removed
        assert not instances.instance_dir(inst["id"]).exists()


class TestInstanzMods:
    def test_mods_getrennt_pro_instanz(self):
        a = _create(name="ModsA")
        b = _create(name="ModsB")
        (instances.mods_dir(a["id"]) / "a.jar").write_bytes(b"a")
        (instances.mods_dir(b["id"]) / "b.jar").write_bytes(b"bb")
        assert [m["filename"] for m in instances.list_mods(a["id"])] == ["a.jar"]
        assert [m["filename"] for m in instances.list_mods(b["id"])] == ["b.jar"]

    def test_delete_mod(self):
        inst = _create(name="DelMod")
        p = instances.mods_dir(inst["id"]) / "weg.jar"
        p.write_bytes(b"x")
        assert instances.delete_mod(inst["id"], "weg.jar") == "weg.jar"
        assert not p.exists()
        with pytest.raises(HTTPException) as e:
            instances.delete_mod(inst["id"], "weg.jar")
        assert e.value.status_code == 404

    def test_delete_mod_traversal_400(self):
        inst = _create(name="Traversal")
        with pytest.raises(HTTPException):
            instances.delete_mod(inst["id"], "../evil.jar")


class TestPackFilename:
    @pytest.mark.parametrize("name", ["Pack v1.2 (Fabric).mrpack", "A.mrpack"])
    def test_gueltig(self, name):
        assert instances.validate_pack_filename(name) == name

    @pytest.mark.parametrize("name", ["pack.jar", ".mrpack", "sub/pack.mrpack",
                                      "..mrpack", "back\\slash.mrpack", "pack.txt"])
    def test_ungueltig(self, name):
        with pytest.raises(HTTPException):
            instances.validate_pack_filename(name)

    def test_zu_lang(self):
        with pytest.raises(HTTPException):
            instances.validate_pack_filename("9" * 300 + ".mrpack")


class TestInstanceApi:
    def test_create_list_detail_delete(self, client, fake_docker):
        resp = client.post("/api/instances", json={
            "name": "API-Srv", "loader": "forge", "game_version": "1.20.1",
            "port": 25601, "memory": "2G", "accept_eula": True})
        assert resp.status_code == 201
        inst = resp.json()
        assert inst["loader"] == "forge"

        listing = client.get("/api/instances").json()["instances"]
        assert any(i["id"] == inst["id"] for i in listing)
        assert "container" in listing[0]

        detail = client.get(f"/api/instances/{inst['id']}").json()
        assert detail["ping"]["online"] is False  # kein Server läuft
        assert detail["mods"] == []

        assert client.delete(f"/api/instances/{inst['id']}").status_code == 200
        assert client.get(f"/api/instances/{inst['id']}").status_code == 404

    def test_eula_fehlt_400(self, client):
        resp = client.post("/api/instances", json={
            "name": "NoEula", "loader": "fabric", "game_version": "1.21.4"})
        assert resp.status_code == 400

    def test_422_bei_schlechter_version(self, client):
        resp = client.post("/api/instances", json={
            "name": "X", "loader": "fabric", "game_version": "1 21 slash",
            "accept_eula": True})
        assert resp.status_code == 422

    def test_unbekannte_instanz_404(self, client):
        assert client.get("/api/instances/gibtsnicht").status_code == 404
        assert client.get("/api/instances/gibtsnicht/mods").status_code == 404

    def test_patch_ram_laufend_erlaubt(self, client, fake_docker):
        """RAM-Änderung ist auch bei laufender Instanz erlaubt (anders als der
        Port, der 409 liefert) — sie wirkt beim nächsten (Neu-)Start."""
        resp = client.post("/api/instances", json={
            "name": "RamPatch-Srv", "loader": "fabric", "game_version": "1.21.4",
            "port": 25621, "accept_eula": True})
        assert resp.status_code == 201
        iid = resp.json()["id"]
        resp = client.patch(f"/api/instances/{iid}", json={"memory": "4G"})
        assert resp.status_code == 200
        assert resp.json()["memory"] == "4G"
        assert instances.get_instance(iid)["memory"] == "4G"

    def test_patch_ram_ungueltig_400(self, client, fake_docker):
        resp = client.post("/api/instances", json={
            "name": "RamBad-Srv", "loader": "fabric", "game_version": "1.21.4",
            "port": 25622, "accept_eula": True})
        assert resp.status_code == 201
        iid = resp.json()["id"]
        resp = client.patch(f"/api/instances/{iid}", json={"memory": "0G"})
        assert resp.status_code == 400
        assert instances.get_instance(iid)["memory"] is None


class TestLiveApi:
    def test_live_ohne_laufende_instanz_leer(self, client):
        resp = client.get("/api/instances/live")
        assert resp.status_code == 200
        assert resp.json() == {"live": []}

    def test_live_ping_nur_laufende(self, client, fake_docker, monkeypatch):
        create = client.post("/api/instances", json={
            "name": "Live-Srv", "loader": "fabric", "game_version": "1.21.4",
            "port": 25611, "accept_eula": True})
        assert create.status_code == 201
        iid = create.json()["id"]

        # Läuft nicht: Container fehlt -> kein Eintrag
        assert client.get("/api/instances/live").json() == {"live": []}

        # Läuft: Fake-Container + gemockter SLP-Ping
        fake_docker.containers._items[f"mc-inst-{iid}"] = FakeContainer(
            f"mc-inst-{iid}", running=True)

        calls = []

        def fake_status(host, port, timeout=1.0):
            calls.append((host, port))
            return {"online": True, "version": "1.21.4", "motd": "Hallo",
                    "players": {"online": 2, "max": 20, "sample": []},
                    "error": None}

        monkeypatch.setattr("app.main.server_status", fake_status)
        data = client.get("/api/instances/live").json()
        assert len(data["live"]) == 1
        entry = data["live"][0]
        assert entry["id"] == iid
        assert entry["name"] == "Live-Srv"
        assert entry["port"] == 25611
        assert entry["ping"]["online"] is True
        assert entry["ping"]["players"]["online"] == 2
        assert calls == [("127.0.0.1", 25611)]  # lokal: Host-Port der Instanz
