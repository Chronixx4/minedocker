"""Tests für den Mod-Sync über AutoModpack (automodpack.py) und die Routen."""
import shutil

import pytest

from app import instances
from app.config import settings

CONF = """# AutoModpack
modpack-host: true
require-modpack: true
connection-mode: HOLEPUNCH
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
    return instances.create_instance("Sync", "fabric", "1.21.4", accept_eula=True)


def url(inst, tail=""):
    return f"/api/instances/{inst['id']}/automodpack{tail}"


class TestStatus:
    def test_nicht_installiert(self, client, inst):
        d = client.get(url(inst)).json()
        assert d["supported"] and not d["installed"] and not d["config"]
        assert d["require"] is None and d["client_mods"] == []
        assert d["install_item"]["project_id"] == "k68glP2e"

    def test_jar_erkannt(self, client, inst):
        mods = instances.mods_dir(inst["id"])
        mods.mkdir(parents=True, exist_ok=True)
        (mods / "automodpack-mc1.21.4-fabric-4.0.0.jar").write_bytes(b"x")
        d = client.get(url(inst)).json()
        assert d["installed"] and d["jar"].startswith("automodpack")

    def test_bukkit_nicht_unterstuetzt(self, client):
        i = instances.create_instance("Paper", "paper", "1.21.4", accept_eula=True)
        assert client.get(url(i)).json()["supported"] is False
        assert client.post(url(i, "/install")).status_code == 400


class TestRequire:
    def test_ohne_config_409(self, client, inst):
        assert client.put(url(inst), json={"require": False}).status_code == 409

    def test_schalter_aendert_nur_eine_zeile(self, client, inst):
        conf = instances.instance_dir(inst["id"]) / "automodpack" / "server.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text(CONF)
        assert client.get(url(inst)).json()["require"] is True
        assert client.put(url(inst), json={"require": False}).json() == {"require": False}
        assert conf.read_text() == CONF.replace("require-modpack: true", "require-modpack: false")
        assert client.get(url(inst)).json()["require"] is False

    def test_legacy_json(self, client, inst):
        conf = instances.instance_dir(inst["id"]) / "automodpack" / "automodpack-server.json"
        conf.parent.mkdir(parents=True)
        conf.write_text('{"requireAutoModpackOnClient": true, "x": 1}')
        client.put(url(inst), json={"require": False})
        assert client.get(url(inst)).json()["require"] is False


class TestClientMods:
    def test_upload_liste_loeschen(self, client, inst):
        r = client.post(url(inst, "/client-mods"),
                        files={"file": ("sodium.jar", b"jar-bytes")})
        assert r.status_code == 200 and r.json()["name"] == "sodium.jar"
        assert client.get(url(inst)).json()["client_mods"] == [{"name": "sodium.jar", "size": 9}]
        assert client.delete(url(inst, "/client-mods/sodium.jar")).status_code == 200
        assert client.get(url(inst)).json()["client_mods"] == []
        assert client.delete(url(inst, "/client-mods/sodium.jar")).status_code == 404

    def test_nur_jar_und_kein_traversal(self, client, inst):
        assert client.post(url(inst, "/client-mods"),
                           files={"file": ("evil.txt", b"x")}).status_code == 400
        assert client.post(url(inst, "/client-mods"),
                           files={"file": ("../evil.jar", b"x")}).status_code == 400


def test_generate_braucht_laufenden_server(client, inst):
    assert client.post(url(inst, "/generate")).status_code == 409


CONF_EXCLUDE = """modpack-host: true
require-modpack: true
exclude: ["**/.*", "**/*.{tmp,disabled,bak}"]
"""


class TestVerteilen:
    def _setup(self, inst, conf=CONF_EXCLUDE):
        mods = instances.mods_dir(inst["id"])
        mods.mkdir(parents=True, exist_ok=True)
        (mods / "automodpack-fabric-4.0.jar").write_bytes(b"x")
        (mods / "lithium.jar").write_bytes(b"x")
        (mods / "serveronly.jar").write_bytes(b"x")
        (mods / "off.jar.disabled").write_bytes(b"x")
        if conf is not None:
            c = instances.instance_dir(inst["id"]) / "automodpack" / "server.conf"
            c.parent.mkdir(parents=True, exist_ok=True)
            c.write_text(conf)
            return c

    def test_liste_und_umschalten(self, client, inst):
        conf = self._setup(inst)
        d = client.get(url(inst)).json()
        assert d["distribute_editable"] is True
        assert d["server_mods"] == [
            {"filename": "lithium.jar", "environment": None, "distribute": True},
            {"filename": "serveronly.jar", "environment": None, "distribute": True}]
        r = client.put(url(inst, "/mods"), json={"filename": "serveronly.jar", "distribute": False})
        assert r.status_code == 200
        assert 'exclude: ["**/.*", "**/*.{tmp,disabled,bak}", "mods/serveronly.jar"]' in conf.read_text()
        assert conf.with_name("server.conf.dashboard-bak").read_text() == CONF_EXCLUDE
        d = client.get(url(inst)).json()
        assert {m["filename"]: m["distribute"] for m in d["server_mods"]}["serveronly.jar"] is False
        client.put(url(inst, "/mods"), json={"filename": "serveronly.jar", "distribute": True})
        assert conf.read_text() == CONF_EXCLUDE

    def test_ohne_anfuehrungszeichen_bleibt_stil(self, client, inst):
        conf = self._setup(inst, "exclude: [**/.*]\n")
        client.put(url(inst, "/mods"), json={"filename": "lithium.jar", "distribute": False})
        assert conf.read_text() == "exclude: [**/.*, mods/lithium.jar]\n"

    def test_mehrdeutige_oder_fehlende_config(self, client, inst):
        self._setup(inst, "exclude: []\nexclude: []\n")
        assert client.get(url(inst)).json()["distribute_editable"] is False
        r = client.put(url(inst, "/mods"), json={"filename": "lithium.jar", "distribute": False})
        assert r.status_code == 409

    def test_eingabe_geprueft(self, client, inst):
        self._setup(inst)
        for name, code in (("fehlt.jar", 404), ("automodpack-fabric-4.0.jar", 400),
                           ("../x.jar", 400)):
            r = client.put(url(inst, "/mods"), json={"filename": name, "distribute": False})
            assert r.status_code == code, name


def test_umgebungs_etikett_aus_mod_metadaten(client, inst):
    import io
    import json
    import zipfile
    mods = instances.mods_dir(inst["id"])
    mods.mkdir(parents=True, exist_ok=True)
    (mods / "automodpack-fabric-4.0.jar").write_bytes(b"x")
    for name, env in (("sodium.jar", "client"), ("both.jar", "*")):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("fabric.mod.json", json.dumps(
                {"schemaVersion": 1, "id": "m", "version": "1", "environment": env}))
        (mods / name).write_bytes(buf.getvalue())
    got = {m["filename"]: m["environment"] for m in client.get(url(inst)).json()["server_mods"]}
    assert got == {"both.jar": "both", "sodium.jar": "client"}


def test_beitrittsadresse(client, inst, monkeypatch):
    monkeypatch.setattr(settings, "public_host", "claw-games.ddns.net")
    d = client.get(url(inst)).json()
    port = instances.get_instance(inst["id"])["port"]
    assert d["join_address"] == f"claw-games.ddns.net:{port}"
    monkeypatch.setattr(settings, "public_host", "")
    assert client.get(url(inst)).json()["join_address"] == ""
