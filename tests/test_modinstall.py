"""Tests für den Modbrowser: Projekt-Details, Installationsplan und
gebündelte Installation mehrerer Mods."""
import asyncio
import shutil

import httpx
import pytest

from app import instances, modinstall, modrinth
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen():
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    yield


@pytest.fixture()
def inst():
    return instances.create_instance("Browser", "fabric", "1.21.4", accept_eula=True)


def _version(vid, project, number, deps=(), vtype="release", changelog="Fixes"):
    return {
        "id": vid, "project_id": project, "version_number": number, "name": number,
        "version_type": vtype, "date_published": "2026-09-01T00:00:00Z",
        "game_versions": ["1.21.4"], "loaders": ["fabric"], "changelog": changelog,
        "files": [{"filename": f"{project.lower()}-{number}.jar", "primary": True,
                   "size": 12, "url": f"https://cdn.modrinth.com/{project}/{number}.jar",
                   "hashes": {}}],
        "dependencies": [{"project_id": d, "dependency_type": "required"} for d in deps],
    }


VERSIONS = {
    "AAA": [_version("a2", "AAA", "2.0", deps=["BBB"]),
            _version("a1", "AAA", "1.0", vtype="beta")],
    "BBB": [_version("b1", "BBB", "0.9")],
    "CCC": [_version("c1", "CCC", "3.1", deps=["BBB"])],
}


def _handler(seen=None):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if seen is not None:
            seen.append((path, dict(request.url.params)))
        if path.startswith("/v2/project/") and path.endswith("/version"):
            pid = path.split("/")[3]
            return httpx.Response(200, json=VERSIONS.get(pid, []))
        if path.startswith("/v2/project/"):
            pid = path.split("/")[3]
            if pid not in VERSIONS:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={
                "id": pid, "slug": pid.lower(), "title": f"Mod {pid}",
                "description": "Beschreibung", "downloads": 1234,
                "categories": ["optimization"], "server_side": "optional",
                "client_side": "required", "icon_url": ""})
        if path.startswith("/v2/version/"):
            vid = path.split("/")[3]
            for versions in VERSIONS.values():
                for v in versions:
                    if v["id"] == vid:
                        return httpx.Response(200, json=v)
            if vid == "fremd":
                return httpx.Response(200, json=_version("fremd", "ZZZ", "1"))
            return httpx.Response(404, json={})
        if request.url.host == "cdn.modrinth.com":
            if "broken" in path:
                return httpx.Response(500)
            return httpx.Response(200, content=b"JAR-" + path.encode()[-8:])
        return httpx.Response(404, json={})
    return handle


@pytest.fixture()
def mock_http(monkeypatch):
    seen: list = []
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        kwargs.setdefault("follow_redirects", True)
        return real_client(transport=httpx.MockTransport(_handler(seen)), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


class TestDetails:
    def test_versionen_gefiltert(self, inst, mock_http):
        data = asyncio.run(modinstall.project_details(inst, "modrinth", "AAA"))
        assert data["project"]["title"] == "Mod AAA"
        assert data["project"]["page_url"] == "https://modrinth.com/mod/aaa"
        assert [v["version_number"] for v in data["versions"]] == ["2.0", "1.0"]
        assert data["versions"][1]["type"] == "beta"
        assert data["versions"][0]["required_dependencies"] == 1
        assert data["versions"][0]["changelog"] == "Fixes"
        params = next(p for path, p in mock_http if path.endswith("/AAA/version"))
        assert params["loaders"] == '["fabric"]'
        assert params["game_versions"] == '["1.21.4"]'

    def test_alle_versionen_ohne_filter(self, inst, mock_http):
        data = asyncio.run(modinstall.project_details(inst, "modrinth", "AAA",
                                                      all_versions=True))
        assert data["filtered"] is False
        params = next(p for path, p in mock_http if path.endswith("/AAA/version"))
        assert "loaders" not in params

    def test_api_route(self, client, inst, mock_http):
        r = client.get(f"/api/instances/{inst['id']}/mods/browse/modrinth/AAA")
        assert r.status_code == 200
        assert r.json()["loader"] == "fabric"
        r = client.get(f"/api/instances/{inst['id']}/mods/browse/ftp/AAA")
        assert r.status_code == 400

    def test_html_changelog_wird_text(self):
        text = modinstall._html_to_text("<p>Neu:</p><ul><li>A &amp; B</li></ul>")
        assert text == "Neu:\nA & B"


class TestPlan:
    def test_gemeinsame_abhaengigkeit_nur_einmal(self, inst, mock_http):
        plan = asyncio.run(modinstall.build_plan(inst, [
            {"source": "modrinth", "project_id": "AAA"},
            {"source": "modrinth", "project_id": "CCC"}]))
        assert [i["filename"] for i in plan["items"]] == ["aaa-2.0.jar", "ccc-3.1.jar"]
        assert [d["filename"] for d in plan["dependencies"]] == ["bbb-0.9.jar"]
        assert plan["total_size"] == 36
        assert plan["errors"] == [] and plan["conflicts"] == []

    def test_feste_version_und_vorgemerkte_abhaengigkeit(self, inst, mock_http):
        plan = asyncio.run(modinstall.build_plan(inst, [
            {"source": "modrinth", "project_id": "AAA", "version_id": "a1"},
            {"source": "modrinth", "project_id": "BBB"}]))
        assert [i["version_number"] for i in plan["items"]] == ["1.0", "0.9"]
        assert plan["dependencies"] == []

    def test_fremde_version_wird_abgelehnt(self, inst, mock_http):
        plan = asyncio.run(modinstall.build_plan(inst, [
            {"source": "modrinth", "project_id": "AAA", "version_id": "fremd"}]))
        assert plan["items"] == []
        assert "gehört nicht" in plan["errors"][0]["detail"]

    def test_konflikt_und_vorhandene_abhaengigkeit(self, inst, mock_http):
        d = instances.mods_dir(inst["id"])
        (d / "aaa-2.0.jar").write_bytes(b"alt")
        (d / "bbb-0.9.jar").write_bytes(b"dep")
        plan = asyncio.run(modinstall.build_plan(inst, [
            {"source": "modrinth", "project_id": "AAA"}]))
        assert plan["conflicts"] == ["aaa-2.0.jar"]
        assert plan["dependencies"] == []
        assert plan["skipped"][0]["reason"] == "Datei existiert bereits"

    def test_api_ohne_urls(self, client, inst, mock_http):
        r = client.post(f"/api/instances/{inst['id']}/mods/plan",
                        json={"items": [{"source": "modrinth", "project_id": "AAA"}]})
        assert r.status_code == 200
        item = r.json()["items"][0]
        assert "url" not in item and "sha1" not in item

    @pytest.mark.parametrize("body", [
        {"items": []},
        {"items": [{"source": "ftp", "project_id": "AAA"}]},
        {"items": [{"source": "modrinth", "project_id": "../x"}]},
        {"items": [{"source": "modrinth", "project_id": f"P{i}"} for i in range(26)]},
    ])
    def test_api_validierung(self, client, inst, body):
        r = client.post(f"/api/instances/{inst['id']}/mods/plan", json=body)
        assert r.status_code == 422


class TestInstall:
    def test_konflikt_409_ohne_overwrite(self, client, inst, mock_http):
        (instances.mods_dir(inst["id"]) / "aaa-2.0.jar").write_bytes(b"alt")
        r = client.post(f"/api/instances/{inst['id']}/mods/install",
                        json={"items": [{"source": "modrinth", "project_id": "AAA"}]})
        assert r.status_code == 409
        assert r.json()["conflicts"] == ["aaa-2.0.jar"]

    def test_overwrite_sichert_alte_datei(self, inst, mock_http, monkeypatch):
        (instances.mods_dir(inst["id"]) / "aaa-2.0.jar").write_bytes(b"alt")
        monkeypatch.setattr(modrinth, "track_task", lambda task: task.cancel())
        result = asyncio.run(modinstall.start_install(
            inst, [{"source": "modrinth", "project_id": "AAA"}], overwrite=True))
        assert [i["filename"] for i in result["items"]] == ["aaa-2.0.jar"]
        assert [d["filename"] for d in result["dependencies"]] == ["bbb-0.9.jar"]
        trash = instances.list_trash(inst["id"])
        assert [(e["filename"], e["reason"]) for e in trash] == [("aaa-2.0.jar", "update")]

    def test_alles_unaufloesbar_400(self, client, inst, mock_http):
        r = client.post(f"/api/instances/{inst['id']}/mods/install",
                        json={"items": [{"source": "modrinth", "project_id": "NOPE"}]})
        assert r.status_code == 400

    def _bundle_job(self, inst, entries):
        job = modrinth.create_job("x", 0, kind="mod", instance_id=inst["id"])
        job["bundle"] = entries
        return job

    def test_bundle_mit_mehreren_mods(self, inst, mock_http):
        d = instances.mods_dir(inst["id"])
        job = self._bundle_job(inst, [
            {"filename": "a.jar", "url": "https://cdn.modrinth.com/A/1.jar", "kind": "item"},
            {"filename": "b.jar", "url": "https://cdn.modrinth.com/broken.jar", "kind": "item"},
            {"filename": "dep.jar", "url": "https://cdn.modrinth.com/D/1.jar", "kind": "dep"},
        ])
        asyncio.run(modrinth.run_bundle_job(job, d))
        assert job["status"] == "done"
        assert (d / "a.jar").is_file() and (d / "dep.jar").is_file()
        assert not (d / "b.jar").exists()
        assert len(job["item_errors"]) == 1 and job["item_errors"][0].startswith("b.jar")
        assert "error" not in job

    def test_bundle_alle_mods_fehlgeschlagen(self, inst, mock_http):
        d = instances.mods_dir(inst["id"])
        job = self._bundle_job(inst, [
            {"filename": "b.jar", "url": "https://cdn.modrinth.com/broken.jar", "kind": "item"},
        ])
        asyncio.run(modrinth.run_bundle_job(job, d))
        assert job["status"] == "error"
        assert "b.jar" in job["error"]

    def test_curseforge_urls_werden_geprueft(self, inst, mock_http):
        d = instances.mods_dir(inst["id"])
        job = self._bundle_job(inst, [
            {"filename": "cf.jar", "url": "https://cdn.modrinth.com/CF/1.jar",
             "kind": "item", "verify_url": True},
        ])
        asyncio.run(modrinth.run_bundle_job(job, d))
        # Modrinth-Host ist kein CurseForge-CDN → abgelehnt (Anti-SSRF)
        assert job["status"] == "error"
        assert not (d / "cf.jar").exists()
