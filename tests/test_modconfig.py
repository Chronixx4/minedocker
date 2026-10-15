"""Tests für Mod-Einstellungen (modconfig.py), zusätzliche Ports (extraports.py)
und die zugehörigen Routen."""
import shutil

import pytest

from app import instances, runtime
from app.config import settings

VOICE = """# Voice Chat
# The port of the voice chat server
port=24454
# Whether the voice chat is active
enable_voice=true
voice_host=
"""

TOML = """# Allgemein
[general]
# Lautstärke
volume = 1.5 # inline
name = "Alt" # Name
list = [1, 2]
enabled = true
"""


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
def inst():
    i = instances.create_instance("Modcfg", "fabric", "1.21.4", accept_eula=True)
    base = instances.instance_dir(i["id"])
    (base / "config" / "voicechat").mkdir(parents=True)
    (base / "config" / "voicechat" / "voicechat-server.properties").write_text(VOICE)
    (base / "config" / "mod.toml").write_text(TOML)
    (base / "config" / "x.json").write_text("{}")
    (base / "config" / "big.toml").write_text("a=1\n" * 200000)  # > 512 KiB
    return i


class TestListe:
    def test_listet_nur_passende_dateien(self, client, inst):
        r = client.get(f"/api/instances/{inst['id']}/modconfigs")
        paths = {f["path"]: f for f in r.json()["files"]}
        assert "config/voicechat/voicechat-server.properties" in paths
        assert paths["config/mod.toml"]["structured"] is True
        assert paths["config/x.json"]["structured"] is False
        assert "config/big.toml" not in paths


class TestLesenSchreiben:
    def test_properties_parsen(self, client, inst):
        r = client.get(f"/api/instances/{inst['id']}/modconfigs/file",
                       params={"path": "config/voicechat/voicechat-server.properties"})
        e = {x["key"]: x for x in r.json()["entries"]}
        assert e["port"]["value"] == "24454" and e["port"]["type"] == "int"
        assert "voice chat server" in e["port"]["comment"]
        assert e["enable_voice"]["type"] == "bool"

    def test_toml_parsen(self, client, inst):
        r = client.get(f"/api/instances/{inst['id']}/modconfigs/file",
                       params={"path": "config/mod.toml"})
        e = {x["key"]: x for x in r.json()["entries"]}
        assert e["volume"]["type"] == "float" and e["volume"]["section"] == "general"
        assert e["name"]["value"] == "Alt" and e["name"]["type"] == "string"
        assert e["list"]["type"] == "raw"

    def test_speichern_erhaelt_kommentare(self, client, inst):
        url = f"/api/instances/{inst['id']}/modconfigs/file"
        data = client.get(url, params={"path": "config/mod.toml"}).json()
        e = {x["key"]: x for x in data["entries"]}
        r = client.put(url, json={"path": "config/mod.toml", "sha": data["sha"], "changes": [
            {"line": e["volume"]["line"], "key": "volume", "value": "2.0"},
            {"line": e["name"]["line"], "key": "name", "value": "Neu"}]})
        assert r.status_code == 200
        text = (instances.instance_dir(inst["id"]) / "config" / "mod.toml").read_text()
        assert "volume = 2.0 # inline" in text
        assert 'name = "Neu" # Name' in text
        assert "# Lautstärke" in text and "list = [1, 2]" in text

    def test_veraltete_datei_409(self, client, inst):
        url = f"/api/instances/{inst['id']}/modconfigs/file"
        path = "config/voicechat/voicechat-server.properties"
        data = client.get(url, params={"path": path}).json()
        (instances.instance_dir(inst["id"]) / path).write_text(VOICE + "extra=1\n")
        r = client.put(url, json={"path": path, "sha": data["sha"], "changes": [
            {"line": 3, "key": "port", "value": "25000"}]})
        assert r.status_code == 409

    def test_falscher_typ_abgelehnt(self, client, inst):
        url = f"/api/instances/{inst['id']}/modconfigs/file"
        path = "config/voicechat/voicechat-server.properties"
        data = client.get(url, params={"path": path}).json()
        r = client.put(url, json={"path": path, "sha": data["sha"], "changes": [
            {"line": 3, "key": "port", "value": "abc"}]})
        assert r.status_code == 400

    def test_pfad_ausserhalb_abgelehnt(self, client, inst):
        url = f"/api/instances/{inst['id']}/modconfigs/file"
        assert client.get(url, params={"path": "server.properties"}).status_code == 400
        assert client.get(url, params={"path": "config/../instance.json"}).status_code in (400, 404)


class TestExtraPorts:
    def test_setzen_und_start(self, client, inst, fake_docker):
        url = f"/api/instances/{inst['id']}/ports"
        r = client.put(url, json={"ports": [{"port": 24454, "proto": "udp", "label": "Voice"}]})
        assert r.status_code == 200
        assert client.get(url).json()["ports"][0]["port"] == 24454
        runtime.start_instance(instances.get_instance(inst["id"]))
        assert fake_docker.containers.run_kwargs["ports"]["24454/udp"] == 24454

    def test_kollision_und_validierung(self, client, inst):
        url = f"/api/instances/{inst['id']}/ports"
        assert client.put(url, json={"ports": [{"port": inst["port"], "proto": "udp"}]}).status_code == 409
        assert client.put(url, json={"ports": [{"port": 80, "proto": "udp"}]}).status_code == 400
        assert client.put(url, json={"ports": [{"port": 30000, "proto": "icmp"}]}).status_code == 400
        other = instances.create_instance("Zweite", "fabric", "1.21.4", accept_eula=True)
        client.put(url, json={"ports": [{"port": 24454, "proto": "udp"}]})
        r = client.put(f"/api/instances/{other['id']}/ports",
                       json={"ports": [{"port": 24454, "proto": "udp"}]})
        assert r.status_code == 409
