"""Tests für app.modrinth: Suchfacets, Download-Auflösung, Job-Lifecycle (gemocktes httpx)."""
import asyncio
import json
import os
import shutil
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import HTTPException

from app import modrinth


def _patch_http(monkeypatch, handler):
    """Ersetzt httpx.AsyncClient durch einen Client mit MockTransport."""
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _handler(routes):
    """routes: {(METHOD, path_suffix): httpx.Response oder callable}."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for (method, suffix), resp in routes.items():
            if request.method == method and path.endswith(suffix):
                return resp(request) if callable(resp) else resp
        return httpx.Response(404, json={"error": "not found"})

    return handle


class TestSearchMods:
    def test_facets_richtig_gesetzt(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["query"] = parse_qs(urlparse(str(request.url)).query)
            return httpx.Response(200, json={"total": 0, "hits": []})

        _patch_http(monkeypatch, handler)
        result = asyncio.run(
            modrinth.search_mods("jei", "fabric", "1.21.4", limit=5, offset=10)
        )
        facets = json.loads(captured["query"]["facets"][0])
        flat = [item for group in facets for item in group]
        assert "project_type:mod" in flat
        assert "categories:fabric" in flat
        assert "versions:1.21.4" in flat
        assert captured["query"]["query"] == ["jei"]
        assert captured["query"]["limit"] == ["5"]
        assert captured["query"]["offset"] == ["10"]
        assert result["loader"] == "fabric"
        assert result["game_version"] == "1.21.4"
        assert result["total"] == 0
        assert result["hits"] == []

    def test_sort_wird_als_index_gesendet(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["query"] = parse_qs(urlparse(str(request.url)).query)
            return httpx.Response(200, json={"total": 0, "hits": []})

        _patch_http(monkeypatch, handler)
        asyncio.run(modrinth.search_mods("x", "fabric", "1.21.4", sort="downloads"))
        assert captured["query"]["index"] == ["downloads"]

    def test_unbekannte_sortierung_400(self):
        with pytest.raises(HTTPException) as e:
            asyncio.run(modrinth.search_mods("x", "fabric", "1.21.4", sort="banana"))
        assert e.value.status_code == 400

    def test_ohne_loader_ohne_version_nur_typ_facet(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["query"] = parse_qs(urlparse(str(request.url)).query)
            return httpx.Response(200, json={"total": 0, "hits": []})

        _patch_http(monkeypatch, handler)
        asyncio.run(modrinth.search_mods("x", None, None))
        facets = json.loads(captured["query"]["facets"][0])
        assert facets == [["project_type:mod"]]

    def test_umgebungsfacette_server_required(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["query"] = parse_qs(urlparse(str(request.url)).query)
            return httpx.Response(200, json={"total": 0, "hits": []})

        _patch_http(monkeypatch, handler)
        asyncio.run(modrinth.search_mods("x", None, None, environment="server_required"))
        facets = json.loads(captured["query"]["facets"][0])
        assert facets == [["project_type:mod"], ["server_side:required"]]

    def test_umgebungsfacette_client_optional_incl_required(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["query"] = parse_qs(urlparse(str(request.url)).query)
            return httpx.Response(200, json={"total": 0, "hits": []})

        _patch_http(monkeypatch, handler)
        asyncio.run(modrinth.search_mods("x", None, None, environment="client"))
        facets = json.loads(captured["query"]["facets"][0])
        assert facets == [["project_type:mod"],
                          ["client_side:required", "client_side:optional"]]

    def test_unbekannte_umgebung_400(self):
        with pytest.raises(HTTPException) as e:
            asyncio.run(modrinth.search_mods("x", None, None, environment="beides"))
        assert e.value.status_code == 400

    def test_total_aus_total_hits(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "total_hits": 75938,
                "hits": [{"project_id": "aaaa", "title": "X"}],
            })

        _patch_http(monkeypatch, handler)
        result = asyncio.run(modrinth.search_mods("x", None, None))
        assert result["total"] == 75938

    def test_hits_feldermapping(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "total": 1,
                "hits": [{
                    "project_id": "aaaa",
                    "slug": "jei",
                    "title": "JEI",
                    "author": "mezz",
                    "description": "Rezept-Anzeige",
                    "downloads": 1234,
                    "icon_url": "",
                    "latest_version": "1.0",
                    "server_side": "required",
                    "unbekanntes_feld": "ignoriert",
                }],
            })

        _patch_http(monkeypatch, handler)
        result = asyncio.run(modrinth.search_mods("", "fabric", "1.21.4"))
        hit = result["hits"][0]
        assert hit["project_id"] == "aaaa"
        assert hit["downloads"] == 1234
        assert "unbekanntes_feld" not in hit

    def test_netzwerkfehler_502(self, monkeypatch):
        def handler(request: httpx.Request):
            raise httpx.ConnectError("boom", request=request)

        _patch_http(monkeypatch, handler)
        with pytest.raises(HTTPException) as e:
            asyncio.run(modrinth.search_mods("x", "fabric", "1.21.4"))
        assert e.value.status_code == 502

    def test_http_fehler_502(self, monkeypatch):
        _patch_http(monkeypatch, _handler({("GET", "/search"): httpx.Response(500)}))
        with pytest.raises(HTTPException) as e:
            asyncio.run(modrinth.search_mods("x", "fabric", "1.21.4"))
        assert e.value.status_code == 502


class TestResolveDownload:
    def test_ohne_version_id_neueste(self, monkeypatch):
        routes = {
            ("GET", "/project/p1/version"): httpx.Response(200, json=[
                {"files": [
                    {"filename": "b.jar", "url": "https://cdn/b.jar", "size": 2},
                    {"filename": "a.jar", "url": "https://cdn/a.jar", "size": 10, "primary": True},
                ]},
            ]),
        }
        _patch_http(monkeypatch, _handler(routes))
        filename, url, size = asyncio.run(
            modrinth.resolve_download("p1", None, "fabric", "1.21.4")
        )
        assert filename == "a.jar"  # primary-Datei bevorzugt
        assert url == "https://cdn/a.jar"
        assert size == 10

    def test_mit_version_id(self, monkeypatch):
        routes = {
            ("GET", "/version/v9"): httpx.Response(200, json={
                "files": [{"filename": "x.jar", "url": "https://cdn/x.jar", "size": 5,
                           "primary": True}],
            }),
        }
        _patch_http(monkeypatch, _handler(routes))
        filename, _url, size = asyncio.run(
            modrinth.resolve_download("p1", "v9", "fabric", "1.21.4")
        )
        assert filename == "x.jar"
        assert size == 5

    def test_keine_kompatible_version_404(self, monkeypatch):
        routes = {("GET", "/project/p1/version"): httpx.Response(200, json=[])}
        _patch_http(monkeypatch, _handler(routes))
        with pytest.raises(HTTPException) as e:
            asyncio.run(modrinth.resolve_download("p1", None, "fabric", "1.21.4"))
        assert e.value.status_code == 404

    def test_keine_jar_datei_404(self, monkeypatch):
        routes = {
            ("GET", "/project/p1/version"): httpx.Response(200, json=[
                {"files": [{"filename": "readme.txt", "url": "https://cdn/r.txt", "size": 1}]},
            ]),
        }
        _patch_http(monkeypatch, _handler(routes))
        with pytest.raises(HTTPException) as e:
            asyncio.run(modrinth.resolve_download("p1", None, "fabric", "1.21.4"))
        assert e.value.status_code == 404


class TestJobLifecycle:
    def test_download_erfolgreich(self, monkeypatch, tmp_path):
        dest = tmp_path / "dl-test.jar"
        payload = b"x" * 100
        _patch_http(monkeypatch, _handler({
            ("GET", "/file.jar"): httpx.Response(200, content=payload),
        }))
        job = modrinth.create_job("dl-test.jar", 100)
        asyncio.run(modrinth.run_download_job(job, "https://cdn/file.jar", dest))
        assert job["status"] == "done"
        assert job["downloaded"] == 100
        assert dest.read_bytes() == payload
        assert not dest.with_suffix(".jar.part").exists()

    def test_download_fehler_cleanup(self, monkeypatch, tmp_path):
        dest = tmp_path / "dl-err.jar"
        _patch_http(monkeypatch, _handler({
            ("GET", "/file.jar"): httpx.Response(500),
        }))
        job = modrinth.create_job("dl-err.jar", 100)
        asyncio.run(modrinth.run_download_job(job, "https://cdn/file.jar", dest))
        assert job["status"] == "error"
        assert "500" in job["error"]
        assert not dest.exists()
        assert not dest.with_suffix(".jar.part").exists()

    def test_max_jobs_bereinigung(self):
        import uuid

        alte_ids = set(modrinth.JOBS)
        for _ in range(modrinth.MAX_JOBS + 5):
            modrinth.create_job(f"j-{uuid.uuid4().hex}.jar", 0)
        assert len(modrinth.JOBS) == modrinth.MAX_JOBS
        assert len(modrinth._JOB_ORDER) == modrinth.MAX_JOBS
        for job_id in list(alte_ids)[:5]:
            assert job_id not in modrinth.JOBS

    def test_get_job_unbekannt(self):
        assert modrinth.get_job("gibtsnicht") is None


class TestUserAgent:
    def test_user_agent_gesetzt(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["ua"] = request.headers.get("user-agent")
            return httpx.Response(200, json={"total": 0, "hits": []})

        _patch_http(monkeypatch, handler)
        asyncio.run(modrinth.search_mods("", "fabric", "1.21.4"))
        assert "mc-dashboard" in captured["ua"]


class TestJobPersistenz:
    """Job-Spiegel auf Platte: Status überlebt Dashboard-Neustarts."""

    @pytest.fixture(autouse=True)
    def _jobs_sauber(self):
        modrinth.JOBS.clear()
        modrinth._JOB_ORDER.clear()
        d = modrinth.jobs_dir()
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
        yield
        modrinth.JOBS.clear()
        modrinth._JOB_ORDER.clear()
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)

    def test_create_job_spiegelt_auf_platte(self):
        job = modrinth.create_job("pack.mrpack", 100, kind="pack",
                                  instance_id="abc12345")
        path = modrinth.jobs_dir() / f"{job['id']}.json"
        assert path.is_file()
        mirrored = json.loads(path.read_text(encoding="utf-8"))
        assert mirrored["status"] == "downloading"
        assert mirrored["instance_id"] == "abc12345"

    def test_download_job_spiegelt_endzustand(self, monkeypatch, tmp_path):
        dest = tmp_path / "spiegel.jar"
        _patch_http(monkeypatch, _handler({("GET", "/file.jar"): httpx.Response(500)}))
        job = modrinth.create_job("spiegel.jar", 100)
        asyncio.run(modrinth.run_download_job(job, "https://cdn/file.jar", dest))
        assert job["status"] == "error"
        mirrored = json.loads(
            (modrinth.jobs_dir() / f"{job['id']}.json").read_text(encoding="utf-8"))
        assert mirrored["status"] == "error"

    def test_restore_markiert_aktive_als_abgebrochen(self):
        job = modrinth.create_job("pack.mrpack", 100, kind="pack",
                                  instance_id="abc12345")
        modrinth.JOBS.clear()  # simuliert Dashboard-Neustart
        modrinth._JOB_ORDER.clear()
        restored = modrinth.restore_jobs()
        assert restored == 1
        loaded = modrinth.get_job(job["id"])
        assert loaded["status"] == "error"
        assert loaded["phase"] == "Abgebrochen (Neustart)"
        assert "Neustart" in loaded["error"]
        assert loaded["instance_id"] == "abc12345"

    def test_fertige_jobs_ueberleben_neustart(self):
        job = modrinth.create_job("m.jar", 5)
        job["status"] = "done"
        job["phase"] = "Fertig"
        modrinth.persist_job(job)
        modrinth.JOBS.clear()
        modrinth._JOB_ORDER.clear()
        modrinth.restore_jobs()
        assert modrinth.get_job(job["id"])["status"] == "done"

    def test_eviction_loescht_spiegeldatei(self):
        import uuid
        for _ in range(modrinth.MAX_JOBS):
            modrinth.create_job(f"j-{uuid.uuid4().hex}.jar", 1)
        old_id = modrinth._JOB_ORDER[0]
        modrinth.create_job("ueberzaehlig.jar", 1)
        assert old_id not in modrinth.JOBS
        assert not (modrinth.jobs_dir() / f"{old_id}.json").exists()

    def test_restore_befolgt_max_jobs(self):
        import uuid
        for _ in range(modrinth.MAX_JOBS + 2):
            modrinth.create_job(f"j-{uuid.uuid4().hex}.jar", 1)
        modrinth.JOBS.clear()
        modrinth._JOB_ORDER.clear()
        restored = modrinth.restore_jobs()
        assert restored == modrinth.MAX_JOBS
        assert len(modrinth.JOBS) == modrinth.MAX_JOBS

    def test_snapshot_entfernt_verwaiste_alte_spiegel(self):
        d = modrinth.jobs_dir()
        d.mkdir(parents=True, exist_ok=True)
        stale = d / "alt007.json"
        stale.write_text("{}", encoding="utf-8")
        alt = time.time() - 8 * 86400  # älter als Aufbewahrung (7 Tage)
        os.utime(stale, (alt, alt))
        modrinth.snapshot_jobs()
        assert not stale.exists()

    def test_defekter_spiegel_wird_verworfen(self):
        d = modrinth.jobs_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "kaputt.json").write_text("kein json", encoding="utf-8")
        assert modrinth.restore_jobs() == 0
        assert not (d / "kaputt.json").exists()
