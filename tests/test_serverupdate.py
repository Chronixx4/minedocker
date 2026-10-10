"""Tests für das Server-Update (Datei hochladen → erkennen → anwenden)."""
import asyncio
import io
import json
import shutil
import uuid
import zipfile

import pytest
from fastapi import HTTPException

from app import instances, runtime, serverupdate
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen(monkeypatch):
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    monkeypatch.setattr(runtime, "is_running", lambda inst: False)
    yield


def _create(loader="forge", game_version="1.20.1"):
    return instances.create_instance(f"Upd-{uuid.uuid4().hex[:8]}", loader,
                                     game_version, accept_eula=True)


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _jar(mod_meta="fabric.mod.json"):
    return _zip({mod_meta: "{}"})


def _analyze(client, inst, name, data):
    return client.post(f"/api/instances/{inst['id']}/update/analyze",
                       files={"file": (name, data)})


def _run(inst, token, **opts):
    """Update anwenden und auf das Job-Ende warten (Job läuft als asyncio-Task,
    deshalb nicht über den TestClient, dessen Loop pro Request endet)."""
    options = {"clean_mods": True, "overwrite_config": True,
               "replace_world": False, "adapt_version": True, **opts}

    async def flow():
        job = await serverupdate.start_apply(inst["id"], token, options)
        for _ in range(300):
            if job["status"] in ("done", "error"):
                return job
            await asyncio.sleep(0.02)
        return job

    return asyncio.run(flow())


class TestErkennung:
    def test_einzelne_mod_jar(self, client):
        inst = _create("fabric", "1.21.4")
        r = _analyze(client, inst, "sodium-0.5.jar", _jar())
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "mods"
        assert body["requires_stop"] is True

    def test_unbekannte_jar_abgelehnt(self, client):
        inst = _create()
        r = _analyze(client, inst, "server.jar", _zip({"a.class": "x"}))
        assert r.status_code == 400

    def test_modrinth_modpack(self, client):
        inst = _create("fabric", "1.21.4")
        index = {"name": "Mein Pack", "dependencies": {
            "minecraft": "1.21.5", "fabric-loader": "0.16.0"}, "files": [
            {"path": "mods/a.jar", "downloads": ["https://cdn.modrinth.com/a.jar"],
             "fileSize": 1, "hashes": {"sha1": "0" * 40}}]}
        r = _analyze(client, inst, "pack.mrpack",
                     _zip({"modrinth.index.json": json.dumps(index)}))
        body = r.json()
        assert body["kind"] == "modpack"
        assert body["version_change"]["to"]["game_version"] == "1.21.5"

    def test_welt(self, client):
        inst = _create()
        r = _analyze(client, inst, "welt.zip",
                     _zip({"MyWorld/level.dat": "x", "MyWorld/region/r.0.0.mca": "y"}))
        assert r.json()["kind"] == "world"

    def test_datapack(self, client):
        inst = _create()
        r = _analyze(client, inst, "dp.zip",
                     _zip({"pack.mcmeta": "{}", "data/x/function/a.mcfunction": ""}))
        assert r.json()["kind"] == "datapack"

    def test_server_pack_mit_version(self, client):
        inst = _create("fabric", "1.21.4")
        z = _zip({
            "Server/mods/a-1.0.jar": "x",
            "Server/config/a.toml": "x=1",
            "Server/libraries/net/minecraftforge/forge/1.20.1-47.2.0/f.jar": "x",
            "Server/server.properties": "motd=hi",
        })
        body = _analyze(client, inst, "srv.zip", z).json()
        assert body["kind"] == "server"
        assert body["detected"]["loader"] == "forge"
        assert body["detected"]["game_version"] == "1.20.1"
        assert body["detected"]["loader_version"] == "47.2.0"
        assert body["version_change"]["to"]["loader"] == "forge"

    def test_neoforge_mc_version(self):
        assert serverupdate._neoforge_mc("21.1.77") == "1.21.1"
        assert serverupdate._neoforge_mc("21.0.5") == "1.21"
        assert serverupdate._neoforge_mc("20.4.200") == "1.20.4"

    def test_nicht_erkannt(self, client):
        inst = _create()
        r = _analyze(client, inst, "x.zip", _zip({"readme.txt": "hi"}))
        assert r.status_code == 400

    def test_falsche_endung(self, client):
        inst = _create()
        assert _analyze(client, inst, "x.exe", b"MZ").status_code == 400


class TestAnwenden:
    def test_mod_ersetzt_aeltere_version(self, client):
        inst = _create("fabric", "1.21.4")
        mods = instances.instance_dir(inst["id"]) / "mods"
        mods.mkdir(exist_ok=True)
        (mods / "sodium-fabric-0.5.8+mc1.21.jar").write_bytes(b"old")
        (mods / "lithium-0.1.jar").write_bytes(b"keep")
        token = _analyze(client, inst, "sodium-fabric-0.6.0+mc1.21.jar",
                         _jar()).json()["token"]
        job = _run(inst, token)
        assert job["status"] == "done", job
        names = sorted(p.name for p in mods.iterdir())
        assert names == ["lithium-0.1.jar", "sodium-fabric-0.6.0+mc1.21.jar"]
        assert job["summary"]["replaced"] == ["sodium-fabric-0.5.8+mc1.21.jar"]
        assert instances.get_instance(inst["id"])["last_update"]["kind"] == "mods"

    def test_server_pack_ersetzt_mods_und_stellt_version_um(self, client):
        inst = _create("fabric", "1.21.4")
        d = instances.instance_dir(inst["id"])
        (d / "mods").mkdir(exist_ok=True)
        (d / "mods" / "alt-1.0.jar").write_bytes(b"old")
        (d / "config").mkdir(exist_ok=True)
        (d / "config" / "mine.toml").write_text("alt")
        props_before = (d / "server.properties").read_text()
        z = _zip({
            "mods/neu-2.0.jar": "n",
            "config/mine.toml": "neu",
            "config/extra.toml": "e",
            "libraries/net/minecraftforge/forge/1.20.1-47.2.0/f.jar": "x",
            "server.properties": "motd=ueberschrieben",
            "run.sh": "#!/bin/sh",
            "forge-server.jar": "x",
        })
        token = _analyze(client, inst, "srv.zip", z).json()["token"]
        job = _run(inst, token)
        assert job["status"] == "done", job
        assert sorted(p.name for p in (d / "mods").iterdir()) == ["neu-2.0.jar"]
        assert (d / "config" / "mine.toml").read_text() == "neu"
        assert (d / "config" / "extra.toml").exists()
        assert (d / "server.properties").read_text() == props_before
        assert not (d / "run.sh").exists()
        assert not (d / "forge-server.jar").exists()
        meta = instances.get_instance(inst["id"])
        assert (meta["loader"], meta["game_version"]) == ("forge", "1.20.1")
        assert job["summary"]["version_adapted"]["to"]["loader"] == "forge"

    def test_config_nicht_ueberschreiben(self, client):
        inst = _create("forge", "1.20.1")
        d = instances.instance_dir(inst["id"])
        (d / "config").mkdir(exist_ok=True)
        (d / "config" / "mine.toml").write_text("alt")
        z = _zip({"mods/a.jar": "x", "config/mine.toml": "neu"})
        token = _analyze(client, inst, "s.zip", z).json()["token"]
        job = _run(inst, token, overwrite_config=False)
        assert job["status"] == "done", job
        assert (d / "config" / "mine.toml").read_text() == "alt"

    def test_welt_im_server_pack_bleibt_standardmaessig(self, client):
        inst = _create("forge", "1.20.1")
        d = instances.instance_dir(inst["id"])
        (d / "world").mkdir(exist_ok=True)
        (d / "world" / "level.dat").write_bytes(b"ALT")
        z = _zip({"mods/a.jar": "x", "world/level.dat": "NEU"})
        token = _analyze(client, inst, "s.zip", z).json()["token"]
        _run(inst, token)
        assert (d / "world" / "level.dat").read_bytes() == b"ALT"
        token = _analyze(client, inst, "s.zip", z).json()["token"]
        job = _run(inst, token, replace_world=True)
        assert job["status"] == "done", job
        assert (d / "world" / "level.dat").read_bytes() == b"NEU"

    def test_welt_ersetzen(self, client):
        inst = _create()
        d = instances.instance_dir(inst["id"])
        (d / "world").mkdir(exist_ok=True)
        (d / "world" / "level.dat").write_bytes(b"ALT")
        token = _analyze(client, inst, "w.zip",
                         _zip({"level.dat": "NEU", "region/r.0.0.mca": "y"})).json()["token"]
        job = _run(inst, token)
        assert job["status"] == "done", job
        assert (d / "world" / "level.dat").read_bytes() == b"NEU"

    def test_laufender_server_verboten(self, client, monkeypatch):
        inst = _create("fabric", "1.21.4")
        token = _analyze(client, inst, "a-1.0.jar", _jar()).json()["token"]
        monkeypatch.setattr(runtime, "is_running", lambda i: True)
        with pytest.raises(HTTPException) as exc:
            _run(inst, token)
        assert exc.value.status_code == 409

    def test_token_fremde_instanz_und_ungueltig(self, client):
        a, b = _create(), _create()
        token = _analyze(client, a, "a-1.0.jar", _jar()).json()["token"]
        with pytest.raises(HTTPException) as exc:
            _run(b, token)
        assert exc.value.status_code == 404
        r = client.post(f"/api/instances/{a['id']}/update/apply",
                        json={"token": "../../etc"})
        assert r.status_code == 422

    def test_verwerfen(self, client):
        inst = _create()
        token = _analyze(client, inst, "a-1.0.jar", _jar()).json()["token"]
        assert client.delete(
            f"/api/instances/{inst['id']}/update/{token}").status_code == 200
        with pytest.raises(HTTPException) as exc:
            _run(inst, token)
        assert exc.value.status_code == 404
