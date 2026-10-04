"""Tests für mehrere Welten je Instanz (Liste, neu, wechseln, kopieren,
umbenennen, löschen, zusätzlich importieren, einzeln herunterladen)."""
import io
import shutil
import uuid
import zipfile

import pytest

from app import instances, runtime
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen(monkeypatch):
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    # Standard: Server gestoppt (kein Docker in Tests)
    monkeypatch.setattr(runtime, "is_running", lambda inst: False)
    yield


def _create(loader="fabric", game_version="1.21.4"):
    return instances.create_instance(f"Welten-{uuid.uuid4().hex[:8]}", loader,
                                     game_version, accept_eula=True)


def _world(d, name, data=b"LEVEL"):
    (d / name / "region").mkdir(parents=True, exist_ok=True)
    (d / name / "level.dat").write_bytes(data)
    (d / name / "region" / "r.0.0.mca").write_bytes(b"x" * 10)


def _props(d):
    return (d / "server.properties").read_text()


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


class TestListe:
    def test_listet_welten_aktive_zuerst(self, client):
        inst = _create()
        d = instances.instance_dir(inst["id"])
        _world(d, "world")
        _world(d, "creative")
        (d / "mods").mkdir(exist_ok=True)
        r = client.get(f"/api/instances/{inst['id']}/worlds")
        assert r.status_code == 200
        data = r.json()
        assert data["active"] == "world"
        assert [w["name"] for w in data["worlds"]] == ["world", "creative"]
        assert data["worlds"][0]["active"] and data["worlds"][0]["size_bytes"] == 15

    def test_paper_nether_end_gehoeren_zur_welt(self, client):
        inst = _create(loader="paper")
        d = instances.instance_dir(inst["id"])
        for name in ("world", "world_nether", "world_the_end"):
            _world(d, name)
        worlds = client.get(f"/api/instances/{inst['id']}/worlds").json()["worlds"]
        assert [w["name"] for w in worlds] == ["world"]
        assert worlds[0]["dimensions"] == ["world_nether", "world_the_end"]
        assert worlds[0]["size_bytes"] == 45

    def test_noch_nicht_erzeugte_aktive_welt(self, client):
        inst = _create()
        r = client.get(f"/api/instances/{inst['id']}/worlds")
        assert r.json()["worlds"] == [{"name": "world", "active": True, "pending": True,
                                       "size_bytes": 0, "modified": None,
                                       "dimensions": []}]


class TestNeuUndWechsel:
    def test_neue_welt_setzt_properties(self, client):
        inst = _create()
        d = instances.instance_dir(inst["id"])
        _world(d, "world")
        r = client.post(f"/api/instances/{inst['id']}/worlds",
                        json={"name": "Abenteuer", "seed": "12345", "level_type": "amplified"})
        assert r.status_code == 200
        props = _props(d)
        assert "level-name=Abenteuer" in props
        assert "level-seed=12345" in props
        assert "level-type=minecraft\\:amplified" in props or \
               "level-type=minecraft:amplified" in props
        assert (d / "world" / "level.dat").exists()  # alte Welt bleibt
        names = [w["name"] for w in client.get(f"/api/instances/{inst['id']}/worlds")
                 .json()["worlds"]]
        assert names == ["Abenteuer", "world"]

    def test_alter_welttyp_ohne_namespace(self, client):
        inst = _create(game_version="1.18.2")
        client.post(f"/api/instances/{inst['id']}/worlds",
                    json={"name": "flach", "level_type": "large_biomes"})
        assert "level-type=largeBiomes" in _props(instances.instance_dir(inst["id"]))

    def test_neue_welt_name_belegt_oder_reserviert(self, client):
        inst = _create()
        d = instances.instance_dir(inst["id"])
        _world(d, "world")
        assert client.post(f"/api/instances/{inst['id']}/worlds",
                           json={"name": "world"}).status_code == 409
        for bad in ("mods", "welt_nether", "../x", "a/b"):
            assert client.post(f"/api/instances/{inst['id']}/worlds",
                               json={"name": bad}).status_code == 400

    def test_wechsel(self, client):
        inst = _create()
        d = instances.instance_dir(inst["id"])
        _world(d, "world")
        _world(d, "creative")
        r = client.post(f"/api/instances/{inst['id']}/worlds/activate",
                        json={"name": "creative"})
        assert r.status_code == 200
        assert "level-name=creative" in _props(d)
        assert client.post(f"/api/instances/{inst['id']}/worlds/activate",
                           json={"name": "fehlt"}).status_code == 404

    def test_wechsel_bei_laufendem_server_verboten(self, client, monkeypatch):
        inst = _create()
        d = instances.instance_dir(inst["id"])
        _world(d, "world")
        _world(d, "creative")
        monkeypatch.setattr(runtime, "is_running", lambda i: True)
        r = client.post(f"/api/instances/{inst['id']}/worlds/activate",
                        json={"name": "creative"})
        assert r.status_code == 409
        assert "level-name=creative" not in _props(d)


class TestKopierenUmbenennenLoeschen:
    def test_kopieren_mit_paper_dimensionen(self, client):
        inst = _create(loader="paper")
        d = instances.instance_dir(inst["id"])
        for name in ("world", "world_nether"):
            _world(d, name)
        (d / "world" / "uid.dat").write_bytes(b"uid")
        (d / "world" / "session.lock").write_bytes(b"lock")
        r = client.post(f"/api/instances/{inst['id']}/worlds/copy",
                        json={"name": "world", "new_name": "test"})
        assert r.status_code == 200
        assert (d / "test" / "level.dat").exists()
        assert (d / "test_nether" / "level.dat").exists()
        assert not (d / "test" / "uid.dat").exists()
        assert not (d / "test" / "session.lock").exists()
        assert (d / "world" / "uid.dat").exists()

    def test_umbenennen_aktiver_welt_passt_level_name_an(self, client):
        inst = _create()
        d = instances.instance_dir(inst["id"])
        _world(d, "world")
        r = client.post(f"/api/instances/{inst['id']}/worlds/rename",
                        json={"name": "world", "new_name": "Hauptwelt"})
        assert r.status_code == 200 and r.json()["active"] is True
        assert (d / "Hauptwelt" / "level.dat").exists() and not (d / "world").exists()
        assert "level-name=Hauptwelt" in _props(d)

    def test_umbenennen_ziel_belegt(self, client):
        inst = _create()
        d = instances.instance_dir(inst["id"])
        _world(d, "world")
        _world(d, "b")
        assert client.post(f"/api/instances/{inst['id']}/worlds/rename",
                           json={"name": "world", "new_name": "b"}).status_code == 409

    def test_loeschen_nur_nicht_aktive(self, client):
        inst = _create()
        d = instances.instance_dir(inst["id"])
        _world(d, "world")
        _world(d, "alt")
        assert client.post(f"/api/instances/{inst['id']}/worlds/delete",
                           json={"name": "world"}).status_code == 409
        r = client.post(f"/api/instances/{inst['id']}/worlds/delete", json={"name": "alt"})
        assert r.status_code == 200 and not (d / "alt").exists()
        assert (d / "world").exists()


class TestImportUndDownload:
    def test_import_als_zusaetzliche_welt(self, client):
        inst = _create()
        d = instances.instance_dir(inst["id"])
        _world(d, "world")
        data = _zip({"Skyblock/level.dat": b"SKY", "Skyblock/region/r.0.0.mca": b"m",
                     "Skyblock/session.lock": b"l"})
        r = client.post(f"/api/instances/{inst['id']}/worlds/import",
                        files={"file": ("skyblock.zip", data, "application/zip")})
        assert r.status_code == 200, r.text
        assert r.json() == {"name": "Skyblock", "active": False}
        assert (d / "Skyblock" / "level.dat").read_bytes() == b"SKY"
        assert not (d / "Skyblock" / "session.lock").exists()
        assert (d / "world" / "level.dat").exists()  # aktive Welt unangetastet

    def test_import_mit_namen_und_aktivieren(self, client):
        inst = _create()
        d = instances.instance_dir(inst["id"])
        data = _zip({"level.dat": b"ROOT"})
        r = client.post(f"/api/instances/{inst['id']}/worlds/import",
                        data={"name": "Neu", "activate": "true"},
                        files={"file": ("x.zip", data, "application/zip")})
        assert r.status_code == 200, r.text
        assert (d / "Neu" / "level.dat").read_bytes() == b"ROOT"
        assert "level-name=Neu" in _props(d)

    def test_import_ohne_level_dat(self, client):
        inst = _create()
        data = _zip({"readme.txt": b"nix"})
        r = client.post(f"/api/instances/{inst['id']}/worlds/import",
                        files={"file": ("x.zip", data, "application/zip")})
        assert r.status_code == 400

    def test_download_einzelner_welt(self, client):
        inst = _create()
        d = instances.instance_dir(inst["id"])
        _world(d, "world")
        _world(d, "creative", b"CREATIVE")
        r = client.get(f"/api/instances/{inst['id']}/worlds/download",
                       params={"name": "creative"})
        assert r.status_code == 200
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            assert zf.read("level.dat") == b"CREATIVE"
        assert client.get(f"/api/instances/{inst['id']}/worlds/download",
                          params={"name": "../etc"}).status_code == 400
