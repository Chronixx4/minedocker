"""Tests für den Versionswechsel (MC-Version/Loader eines Servers ändern)."""
import asyncio
import hashlib
import json
import shutil

import httpx
import pytest
from fastapi import HTTPException

from app import backups, instances, modrinth, runtime, versionchange
from app.config import settings

KEEP = b"keeper-1.21.4"
OLD = b"oldmod-1.21.4"
NEW_JAR = b"keeper-1.21.5-bytes"


@pytest.fixture(autouse=True)
def _leere_instanzen():
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    yield


@pytest.fixture()
def instanz():
    inst = instances.create_instance("Wechsel-Srv", "fabric", "1.21.4", accept_eula=True)
    mods = instances.mods_dir(inst["id"])
    mods.mkdir(parents=True, exist_ok=True)
    (mods / "keeper-1.0.jar").write_bytes(KEEP)
    (mods / "oldmod-1.0.jar").write_bytes(OLD)
    (mods / "custom.jar").write_bytes(b"selbst gebaut")
    return inst


def _version(vid, project, number, gv, sha, filename, url):
    return {"id": vid, "project_id": project, "version_number": number,
            "date_published": "2026-01-01T00:00:00Z", "game_versions": [gv],
            "loaders": ["fabric"],
            "files": [{"filename": filename, "primary": True, "url": url,
                       "size": len(NEW_JAR), "hashes": {"sha1": sha}}]}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if "/version_file/" in path:
        sha = path.rsplit("/", 1)[1]
        table = {
            hashlib.sha1(KEEP).hexdigest(): [_version(
                "k1", "KEEP", "1.0", "1.21.4", hashlib.sha1(KEEP).hexdigest(),
                "keeper-1.0.jar", "https://cdn.modrinth.com/k1.jar")],
            hashlib.sha1(OLD).hexdigest(): [_version(
                "o1", "OLD", "1.0", "1.21.4", hashlib.sha1(OLD).hexdigest(),
                "oldmod-1.0.jar", "https://cdn.modrinth.com/o1.jar")],
        }
        return httpx.Response(200, json=table[sha]) if sha in table else httpx.Response(404)
    if path.endswith("/project/KEEP/version"):
        gv = json.loads(request.url.params["game_versions"])[0]
        if gv == "1.21.5":
            return httpx.Response(200, json=[_version(
                "k2", "KEEP", "2.0", "1.21.5", hashlib.sha1(NEW_JAR).hexdigest(),
                "keeper-2.0.jar", "https://cdn.modrinth.com/k2.jar")])
        return httpx.Response(200, json=[])
    if path.endswith("/project/OLD/version"):
        return httpx.Response(200, json=[])
    if path.endswith("/k2.jar"):
        return httpx.Response(200, content=NEW_JAR)
    return httpx.Response(404)


@pytest.fixture()
def _mock_http(monkeypatch):
    real = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real(transport=httpx.MockTransport(_handler), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", factory)


class TestHilfen:
    @pytest.mark.parametrize("cur,target,down", [
        ("1.21.4", "1.20.1", True), ("1.21.4", "1.21.5", False),
        ("1.21", "1.21.0", False), ("1.21.4", "25w14a", False),
    ])
    def test_downgrade(self, cur, target, down):
        assert versionchange.is_downgrade(cur, target) is down

    def test_validierung(self):
        with pytest.raises(HTTPException):
            versionchange.validate_target("lite", "1.21.4")
        with pytest.raises(HTTPException):
            versionchange.validate_target("fabric", "../x")
        assert versionchange.validate_target(" Fabric ", "1.21.5", " ") == \
            ("fabric", "1.21.5", None)


class TestVorschau:
    def test_mods_einsortiert(self, instanz, _mock_http):
        result = asyncio.run(versionchange.check(instanz["id"], "fabric", "1.21.5"))
        mods = result["mods"]
        assert [m["filename"] for m in mods["available"]] == ["keeper-1.0.jar"]
        assert mods["available"][0]["to"] == "2.0"
        assert [m["filename"] for m in mods["missing"]] == ["oldmod-1.0.jar"]
        assert [m["filename"] for m in mods["unknown"]] == ["custom.jar"]
        assert result["downgrade"] is False and result["same"] is False

    def test_api(self, client, instanz, _mock_http):
        r = client.post(f"/api/instances/{instanz['id']}/version/check",
                        json={"loader": "fabric", "game_version": "1.20.1"})
        assert r.status_code == 200, r.text
        assert r.json()["downgrade"] is True


class TestWechsel:
    def test_kompletter_ablauf(self, instanz, _mock_http, fake_docker):
        job = asyncio.run(_start_and_wait(instanz["id"]))
        assert job["status"] == "done", job
        meta = instances.get_instance(instanz["id"])
        assert (meta["loader"], meta["game_version"]) == ("fabric", "1.21.5")
        mods = instances.mods_dir(instanz["id"])
        assert (mods / "keeper-2.0.jar").read_bytes() == NEW_JAR
        assert not (mods / "keeper-1.0.jar").exists()
        assert (mods / "oldmod-1.0.jar.disabled").exists()
        assert (mods / "custom.jar").exists()
        assert job["summary"]["disabled"] == ["oldmod-1.0.jar"]
        assert job["summary"]["updated"] == 1
        names = [b["name"] for b in backups.list_backups(instanz["id"])]
        assert job["backup"] in names

    def test_laufender_server_abgelehnt(self, instanz, fake_docker):
        runtime.start_instance(instanz)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(versionchange.start_change(instanz["id"], "fabric", "1.21.5"))
        assert exc.value.status_code == 409

    def test_downgrade_braucht_bestaetigung(self, instanz, fake_docker):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(versionchange.start_change(instanz["id"], "fabric", "1.20.1"))
        assert exc.value.status_code == 409

    def test_gleiche_version_abgelehnt(self, instanz, fake_docker):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(versionchange.start_change(instanz["id"], "fabric", "1.21.4"))
        assert exc.value.status_code == 400

    def test_backup_fehler_aendert_nichts(self, instanz, _mock_http, fake_docker, monkeypatch):
        def kaputt(*a, **k):
            raise OSError("Platte voll")
        monkeypatch.setattr(backups, "safety_backup", kaputt)
        job = asyncio.run(_start_and_wait(instanz["id"]))
        assert job["status"] == "error" and "Platte voll" in job["error"]
        assert instances.get_instance(instanz["id"])["game_version"] == "1.21.4"


async def _start_and_wait(instance_id):
    job = await versionchange.start_change(instance_id, "fabric", "1.21.5")
    for _ in range(200):
        if job["status"] in ("done", "error"):
            break
        await asyncio.sleep(0.02)
    return modrinth.get_job(job["id"]) or job
