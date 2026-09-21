"""Tests für die CurseForge-Integration (app.curseforge, Endpunkte, Pack-Install).

Abgedeckt: API-Key-Guard (503), Suchparameter (gameId/classId/Loader-Typ/Key-Header),
Datei-Auflösung (neueste stabile Datei, Fallback-URL), Einzel-Mod-Download,
Pack-Installation via CF-API (Zip → manifest → Overrides + Mods) und Fehlerfälle.
"""
import asyncio
import collections
import hashlib
import io
import json
import shutil
import time
import zipfile

import httpx
import pytest
from fastapi import HTTPException

from app import curseforge, instances, modrinth, packs
from app.config import settings

TEST_KEY = "test-cf-key-123"


@pytest.fixture(autouse=True)
def _umgebung(monkeypatch):
    """Saubere Instanz-/Job-Umgebung + CF-API-Key für alle Tests."""
    monkeypatch.setattr(settings, "cf_api_key", TEST_KEY)
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    modrinth.JOBS.clear()
    modrinth._JOB_ORDER.clear()
    yield
    modrinth.JOBS.clear()
    modrinth._JOB_ORDER.clear()


def _patch_http(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        kwargs.setdefault("follow_redirects", True)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(packs, "_new_client", factory)


def _handler(routes):
    """routes: {path_suffix: (status, content)} — content bytes oder dict→json."""

    def handle(request: httpx.Request) -> httpx.Response:
        for suffix, (status, content) in routes.items():
            if request.url.path.endswith(suffix):
                if isinstance(content, (dict, list)):
                    return httpx.Response(status, json=content)
                return httpx.Response(status, content=content)
        return httpx.Response(404, json={"errorCode": 404})

    return handle


JAR = b"cf-fake-jar" * 20
CFG = b"cf-config=1\n"


def _mod(class_id=6):
    return {
        "id": 123,
        "gameId": 432,
        "name": "CF Mod",
        "slug": "cfmod",
        "summary": "Ein Test-Mod",
        "downloadCount": 4242,
        "classId": class_id,
        "dateModified": "2025-06-01T00:00:00Z",
        "logo": {"thumbnailUrl": "https://cdn.example/icon.png"},
        "authors": [{"name": "Tester"}],
    }


def _file(fid, name, date, size, release=1, url=None, sha1=None):
    return {
        "id": fid,
        "fileName": name,
        "fileDate": date,
        "fileSize": size,
        "releaseType": release,
        "gameVersions": ["1.21.4"],
        "downloadUrl": url,
        "hashes": {"sha1": sha1 if sha1 is not None
                   else hashlib.sha1(JAR).hexdigest()},
    }


# ---------------------------------------------------------------------------
# Key-Guard
# ---------------------------------------------------------------------------

class TestKeyGuard:
    def test_ohne_key_503(self, monkeypatch):
        monkeypatch.setattr(settings, "cf_api_key", "")
        with pytest.raises(HTTPException) as e:
            asyncio.run(curseforge.search_mods("x", "fabric", "1.21.4"))
        assert e.value.status_code == 503
        assert "console.curseforge.com" in e.value.detail

    def test_ohne_key_resolve_pack_503(self, monkeypatch):
        monkeypatch.setattr(settings, "cf_api_key", "")
        with pytest.raises(HTTPException) as e:
            asyncio.run(curseforge.resolve_pack("cfmod"))
        assert e.value.status_code == 503

    def test_search_route_ohne_key_503(self, client, monkeypatch):
        monkeypatch.setattr(settings, "cf_api_key", "")
        resp = client.get("/api/curseforge/search", params={"q": "x"})
        assert resp.status_code == 503

    def test_download_route_ohne_key_503(self, client, monkeypatch):
        monkeypatch.setattr(settings, "cf_api_key", "")
        resp = client.post("/api/curseforge/download", json={"project_id": "cfmod"})
        assert resp.status_code == 503

    def test_install_route_ohne_key_503(self, client, monkeypatch):
        monkeypatch.setattr(settings, "cf_api_key", "")
        inst = instances.create_instance("NoKey", "fabric", "1.21.4",
                                         accept_eula=True)
        resp = client.post(f"/api/instances/{inst['id']}/modpacks/install",
                           json={"project_id": "cfmod", "source": "curseforge"})
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Suche
# ---------------------------------------------------------------------------

class TestSearchMods:
    def test_parameter_und_key_header(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            captured["key"] = request.headers.get("x-api-key")
            captured["ua"] = request.headers.get("user-agent")
            return httpx.Response(200, json={"data": [], "pagination": {
                "totalCount": 0}})

        _patch_http(monkeypatch, handler)
        result = asyncio.run(curseforge.search_mods("jei", "fabric", "1.21.4",
                                                    offset=20))
        p = captured["params"]
        assert p["gameId"] == "432"
        assert p["classId"] == "6"
        assert p["gameVersion"] == "1.21.4"
        assert p["modLoaderType"] == "4"  # fabric
        assert p["searchFilter"] == "jei"
        assert p["sortField"] == "2"
        assert p["sortOrder"] == "desc"
        assert p["pageNumber"] == "1"  # offset 20 → Seite 2
        assert captured["key"] == TEST_KEY
        assert "mc-dashboard" in captured["ua"]
        assert result["source"] == "curseforge"
        assert result["loader"] == "fabric"

    def test_sortfield_mapping(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"data": [], "pagination": {}})

        _patch_http(monkeypatch, handler)
        for sort, field in (("relevance", "2"), ("downloads", "6"),
                            ("updated", "3"), ("newest", "9")):
            asyncio.run(curseforge.search_mods("x", "fabric", "1.21.4", sort=sort))
            assert captured["params"]["sortField"] == field

    def test_unbekannte_sortierung_400(self, monkeypatch):
        _patch_http(monkeypatch, _handler({"/mods/search": (200, {"data": []})}))
        with pytest.raises(HTTPException) as e:
            asyncio.run(curseforge.search_mods("x", "fabric", "1.21.4", sort="banana"))
        assert e.value.status_code == 400

    def test_ohne_loader_ohne_version_keine_filter(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"data": [], "pagination": {}})

        _patch_http(monkeypatch, handler)
        asyncio.run(curseforge.search_mods("x", None, None))
        p = captured["params"]
        assert "modLoaderType" not in p
        assert "gameVersion" not in p

    def test_hit_normalisierung(self, monkeypatch):
        _patch_http(monkeypatch, _handler({
            "/mods/search": (200, {"data": [_mod()],
                                   "pagination": {"totalCount": 1}}),
        }))
        result = asyncio.run(curseforge.search_mods("cf", "forge", "1.21.4"))
        hit = result["hits"][0]
        assert hit["project_id"] == "123"
        assert hit["slug"] == "cfmod"
        assert hit["title"] == "CF Mod"
        assert hit["author"] == "Tester"
        assert hit["downloads"] == 4242
        assert hit["icon_url"] == "https://cdn.example/icon.png"
        assert hit["server_side"] is None

    def test_pagination_aus_response(self, monkeypatch):
        _patch_http(monkeypatch, _handler({
            "/mods/search": (200, {"data": [_mod()],
                                   "pagination": {"totalCount": 37}}),
        }))
        result = asyncio.run(curseforge.search_mods("", "fabric", "1.21.4"))
        assert result["total"] == 37

    def test_nicht_unterstuetzter_loader_400(self):
        with pytest.raises(HTTPException) as e:
            asyncio.run(curseforge.search_mods("x", "paper", "1.21.4"))
        assert e.value.status_code == 400
        assert "paper" in e.value.detail

    def test_netzwerkfehler_502(self, monkeypatch):
        def handler(request: httpx.Request):
            raise httpx.ConnectError("boom", request=request)

        _patch_http(monkeypatch, handler)
        with pytest.raises(HTTPException) as e:
            asyncio.run(curseforge.search_mods("x", "fabric", "1.21.4"))
        assert e.value.status_code == 502

    def test_http_fehler_502(self, monkeypatch):
        _patch_http(monkeypatch, _handler({"/mods/search": (500, b"err")}))
        with pytest.raises(HTTPException) as e:
            asyncio.run(curseforge.search_mods("x", "fabric", "1.21.4"))
        assert e.value.status_code == 502


class TestSearchModpacks:
    def test_classId_4471_und_kein_loader_filter(self, monkeypatch):
        inst = instances.create_instance("CfPackSearch", "fabric", "1.21.4",
                                         accept_eula=True)
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"data": [_mod(4471)],
                                             "pagination": {"totalCount": 1}})

        _patch_http(monkeypatch, handler)
        result = asyncio.run(
            curseforge.search_modpacks(inst, "atm"))
        p = captured["params"]
        assert p["classId"] == "4471"
        assert p["gameVersion"] == "1.21.4"
        assert "modLoaderType" not in p  # Packs: Loader steckt im manifest
        hit = result["hits"][0]
        assert hit["compatible"] is None
        assert hit["loaders"] == []
        assert result["instance"]["id"] == inst["id"]

    def test_pack_suche_route(self, client, monkeypatch):
        inst = instances.create_instance("CfPackApi", "fabric", "1.21.4",
                                         accept_eula=True)
        _patch_http(monkeypatch, _handler({
            "/mods/search": (200, {"data": [_mod(4471)],
                                   "pagination": {"totalCount": 1}}),
        }))
        resp = client.get(f"/api/instances/{inst['id']}/modpacks/search",
                          params={"q": "atm", "source": "curseforge"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "curseforge"
        assert data["hits"][0]["title"] == "CF Mod"


# ---------------------------------------------------------------------------
# Datei-Auflösung (Einzel-Mod)
# ---------------------------------------------------------------------------

_FILES = [
    _file(900, "cfmod-old.jar", "2024-01-01T00:00:00Z", len(JAR), release=1),
    _file(901, "cfmod-beta.jar", "2025-06-01T00:00:00Z", len(JAR), release=2),
    _file(902, "cfmod-src.txt", "2025-07-01T00:00:00Z", 12, release=1),
]


class TestResolveDownload:
    def test_slug_wird_aufgeloest_und_stabile_datei_gewaehlt(self, monkeypatch):
        _patch_http(monkeypatch, _handler({
            "/mods/search": (200, {"data": [_mod()], "pagination": {}}),
            "/mods/123/files": (200, {"data": _FILES, "pagination": {}}),
        }))
        filename, url, size, sha1 = asyncio.run(
            curseforge.resolve_download("cfmod", None, "fabric", "1.21.4"))
        # Stabile Release trotz neuerer Beta; .txt wird nicht bevorzugt
        assert filename == "cfmod-old.jar"
        assert size == len(JAR)
        # SHA1 aus der CF-API wird mitgeliefert (für Download-Verifikation)
        assert sha1 == hashlib.sha1(JAR).hexdigest()
        # downloadUrl=None → Fallback auf Website-Redirect
        assert url == "https://www.curseforge.com/api/v1/mods/123/files/900/download"

    def test_downloadurl_wird_genutzt(self, monkeypatch):
        files = [_file(900, "cfmod.jar", "2024-01-01T00:00:00Z", len(JAR),
                       url="https://mediafilez.forgecdn.net/files/1/cfmod.jar")]
        _patch_http(monkeypatch, _handler({
            "/mods/search": (200, {"data": [_mod()], "pagination": {}}),
            "/mods/123/files": (200, {"data": files, "pagination": {}}),
        }))
        _, url, _, _ = asyncio.run(
            curseforge.resolve_download("cfmod", None, "fabric", "1.21.4"))
        assert url == "https://mediafilez.forgecdn.net/files/1/cfmod.jar"

    def test_fremder_download_host_faellt_zurueck(self, monkeypatch):
        """downloadUrl mit Nicht-CDN-Host wird verworfen (Anti-SSRF) → Fallback."""
        files = [_file(900, "cfmod.jar", "2024-01-01T00:00:00Z", len(JAR),
                       url="https://evil.example/files/1/cfmod.jar")]
        _patch_http(monkeypatch, _handler({
            "/mods/search": (200, {"data": [_mod()], "pagination": {}}),
            "/mods/123/files": (200, {"data": files, "pagination": {}}),
        }))
        _, url, _, _ = asyncio.run(
            curseforge.resolve_download("cfmod", None, "fabric", "1.21.4"))
        assert url == "https://www.curseforge.com/api/v1/mods/123/files/900/download"

    def test_explizite_file_id(self, monkeypatch):
        _patch_http(monkeypatch, _handler({
            "/mods/search": (200, {"data": [_mod()], "pagination": {}}),
            "/mods/123/files": (200, {"data": _FILES, "pagination": {}}),
        }))
        filename, _, _, _ = asyncio.run(
            curseforge.resolve_download("cfmod", "902", "fabric", "1.21.4"))
        assert filename == "cfmod-src.txt"

    def test_fremde_file_id_404(self, monkeypatch):
        _patch_http(monkeypatch, _handler({
            "/mods/search": (200, {"data": [_mod()], "pagination": {}}),
            "/mods/123/files": (200, {"data": _FILES, "pagination": {}}),
        }))
        with pytest.raises(HTTPException) as e:
            asyncio.run(curseforge.resolve_download("cfmod", "999", "fabric", "1.21.4"))
        assert e.value.status_code == 404

    def test_keine_dateien_404(self, monkeypatch):
        _patch_http(monkeypatch, _handler({
            "/mods/search": (200, {"data": [_mod()], "pagination": {}}),
            "/mods/123/files": (200, {"data": [], "pagination": {}}),
        }))
        with pytest.raises(HTTPException) as e:
            asyncio.run(curseforge.resolve_download("cfmod", None, "fabric", "1.21.4"))
        assert e.value.status_code == 404

    def test_unbekannter_slug_404(self, monkeypatch):
        _patch_http(monkeypatch, _handler({
            "/mods/search": (200, {"data": [], "pagination": {}}),
        }))
        with pytest.raises(HTTPException) as e:
            asyncio.run(curseforge.resolve_download("gibtsnicht", None,
                                                    "fabric", "1.21.4"))
        assert e.value.status_code == 404

    def test_full_liefert_relationen(self, monkeypatch):
        """resolve_download_full liefert Pflicht-Abhängigkeiten aus der Datei."""
        files = [dict(_file(900, "cfmod.jar", "2024-01-01T00:00:00Z", len(JAR)),
                      relations={"projects": [
                          {"id": 306612, "relationType": 3},   # required
                          {"id": 459701, "relationType": 2},   # optional
                          {"id": 220818, "relationType": 1},   # embedded
                      ]})]
        _patch_http(monkeypatch, _handler({
            "/mods/search": (200, {"data": [_mod()], "pagination": {}}),
            "/mods/123/files": (200, {"data": files, "pagination": {}}),
        }))
        filename, _, _, _, dep_ids = asyncio.run(
            curseforge.resolve_download_full("cfmod", None, "fabric", "1.21.4"))
        assert filename == "cfmod.jar"
        assert dep_ids == ["306612"]
        # resolve_download (Kompatibilitäts-Wrapper) liefert 4 Werte
        r4 = asyncio.run(curseforge.resolve_download("cfmod", None, "fabric", "1.21.4"))
        assert len(r4) == 4


class TestRequiredDepIds:
    def test_leer_ohne_relations(self):
        assert curseforge.required_dep_ids(_file(1, "a.jar", "2024", 10)) == []

    def test_defekt_wird_toleriert(self):
        assert curseforge.required_dep_ids({"relations": None}) == []
        assert curseforge.required_dep_ids({"relations": {"projects": "kaputt"}}) == []
        assert curseforge.required_dep_ids({"relations": {"projects": [
            {"id": 1, "relationType": None}, "x", {}, {"relationType": 3},
        ]}}) == []
        assert curseforge.required_dep_ids({"relations": {"projects": [
            {"id": 5, "relationType": "3"}]}}) == ["5"]


class TestCfDependencyFiles:
    @staticmethod
    def _project(mod_id, slug=None):
        m = _mod()
        m["id"] = mod_id
        if slug:
            m["slug"] = slug
        return m

    def test_deps_werden_aufgeloest_rekursiv_und_gefiltert(self, monkeypatch):
        # Pflicht-Deps des Aufrufs: 306612 und 459701;
        # 306612 hat selbst eine Pflicht-Dep (300) → Tiefe 2;
        # 459701 liefert keine Dateien → skipped; 200 ist "bereits installiert".
        dep_files = [
            dict(_file(1000, "fabric-api.jar", "2025-01-01T00:00:00Z", 2000),
                 relations={"projects": [{"id": 300, "relationType": 3}]}),
        ]
        sub_files = [_file(2000, "cloth-config.jar", "2025-02-01T00:00:00Z", 500)]
        routes = {
            "/mods/search": (200, {"data": [_mod()], "pagination": {}}),
            "/mods/306612/files": (200, {"data": dep_files, "pagination": {}}),
            "/mods/300/files": (200, {"data": sub_files, "pagination": {}}),
            "/mods/459701/files": (200, {"data": [], "pagination": {}}),
            "/mods/306612": (200, {"data": self._project(306612)}),
            "/mods/300": (200, {"data": self._project(300)}),
            "/mods/459701": (200, {"data": self._project(459701)}),
        }

        def handler(request: httpx.Request) -> httpx.Response:
            for suffix, (status, content) in routes.items():
                if request.url.path.endswith(suffix):
                    return httpx.Response(status, json=content)
            return httpx.Response(404, json={"errorCode": 404})

        _patch_http(monkeypatch, handler)
        result = asyncio.run(curseforge.dependency_files(
            ["306612", "459701", "200"], "fabric", "1.21.4", {"200"}))
        names = [f["filename"] for f in result["files"]]
        assert names == ["fabric-api.jar", "cloth-config.jar"]
        assert all(f["url"].startswith("https://") for f in result["files"])
        assert all(f["project_id"] in ("306612", "300") for f in result["files"])
        reasons = {s["project_id"]: s["reason"] for s in result["skipped"]}
        assert reasons["200"] == "bereits installiert"
        assert "459701" in reasons  # keine kompatible Datei

    def test_leere_dep_liste(self, monkeypatch):
        _patch_http(monkeypatch, _handler({"/mods/search": (200, {"data": [], "pagination": {}})}))
        result = asyncio.run(curseforge.dependency_files([], "fabric", "1.21.4", set()))
        assert result == {"files": [], "skipped": []}

    def test_download_route_mit_deps(self, client, monkeypatch):
        """Instanz-Ziel: Pflicht-Deps aus Relations werden mitinstalliert."""
        inst = instances.create_instance("CfDepsRoute", "fabric", "1.21.4",
                                         accept_eula=True)
        dep_file = _file(1000, "fabric-api.jar", "2025-01-01T00:00:00Z", len(JAR),
                         url="https://mediafilez.forgecdn.net/files/1/fabric-api.jar")
        main_file = dict(_file(900, "cfmod.jar", "2024-01-01T00:00:00Z", len(JAR),
                               url="https://mediafilez.forgecdn.net/files/1/cfmod.jar"),
                         relations={"projects": [{"id": 306612, "relationType": 3}]})
        routes = {
            "/mods/search": (200, {"data": [_mod()], "pagination": {}}),
            "/mods/123/files": (200, {"data": [main_file], "pagination": {}}),
            "/mods/306612/files": (200, {"data": [dep_file], "pagination": {}}),
            "/mods/306612": (200, {"data": {"id": 306612, "slug": "fabric-api",
                                            "name": "Fabric API"}}),
            "/files/1/cfmod.jar": (200, JAR),
            "/files/1/fabric-api.jar": (200, JAR),
        }

        def handler(request: httpx.Request) -> httpx.Response:
            for suffix, (status, content) in routes.items():
                if request.url.path.endswith(suffix):
                    if isinstance(content, (dict, list)):
                        return httpx.Response(status, json=content)
                    return httpx.Response(status, content=content)
            return httpx.Response(404, json={"errorCode": 404})

        _patch_http(monkeypatch, handler)
        resp = client.post("/api/curseforge/download",
                           json={"project_id": "cfmod", "instance_id": inst["id"]})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert [d["filename"] for d in data["dependencies"]] == ["fabric-api.jar"]
        for _ in range(200):
            job = client.get(f"/api/jobs/{data['job_id']}").json()
            if job["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert job["status"] == "done", job.get("error")
        assert not job.get("dep_errors")
        mods = instances.list_mods(inst["id"])
        assert sorted(m["filename"] for m in mods) == ["cfmod.jar", "fabric-api.jar"]


class TestResolvePack:
    def test_zip_dateiname_und_auswahl(self, monkeypatch):
        pack = _cf_zip()
        pack_file = _file(700, "CFPack", "2025-01-01T00:00:00Z", 2048,
                          sha1=hashlib.sha1(pack).hexdigest())
        _patch_http(monkeypatch, _handler({
            "/mods/search": (200, {"data": [_mod(4471)], "pagination": {}}),
            "/mods/123/files": (200, {"data": [pack_file], "pagination": {}}),
        }))
        mod, filename, url, size, sha1, chosen_id = asyncio.run(
            curseforge.resolve_pack("cfmod"))
        assert mod["slug"] == "cfmod"
        assert filename == "CFPack.zip"  # .zip ergänzt
        assert url.endswith("/mods/123/files/700/download")
        assert size == 2048
        assert sha1 == hashlib.sha1(_cf_zip()).hexdigest()
        assert chosen_id == "700"

    def test_explizite_pack_datei(self, monkeypatch):
        files = [
            _file(700, "CFPack.zip", "2025-01-01T00:00:00Z", 2048),
            _file(701, "CFPack-old.zip", "2024-01-01T00:00:00Z", 2048),
        ]
        _patch_http(monkeypatch, _handler({
            "/mods/search": (200, {"data": [_mod(4471)], "pagination": {}}),
            "/mods/123/files": (200, {"data": files, "pagination": {}}),
        }))
        _, filename, _, _, _, chosen_id = asyncio.run(
            curseforge.resolve_pack("cfmod", "701"))
        assert filename == "CFPack-old.zip"
        assert chosen_id == "701"


# ---------------------------------------------------------------------------
# Einzel-Mod-Download (Endpunkt)
# ---------------------------------------------------------------------------

def _await_job(coro):
    async def _flow():
        job = await coro
        for _ in range(500):
            if job["status"] in ("done", "error"):
                return job
            await asyncio.sleep(0.02)
        return job

    return asyncio.run(_flow())


def _mod_routes():
    return {
        "/mods/search": (200, {"data": [_mod()], "pagination": {}}),
        "/mods/123/files": (200, {"data": _FILES, "pagination": {}}),
        "/cfmod-old.jar": (200, JAR),
        "/testmod.jar": (200, JAR),
    }


def _redirect_handler(routes):
    base = _handler(routes)
    # CurseForge-Website-Redirects → CDN-Dateinamen
    redirects = {
        "/mods/111/files/222/download": "testmod.jar",
        "/mods/123/files/700/download": "CFPack.zip",
        "/mods/123/files/701/download": "CFPack-old.zip",
        "/mods/123/files/900/download": "cfmod-old.jar",
    }

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for suffix, name in redirects.items():
            if path.endswith(suffix):
                return httpx.Response(302, headers={
                    "Location": f"https://mediafilez.forgecdn.net/files/9/{name}"})
        return base(request)

    return handle


class TestModDownloadApi:
    def test_download_hauptserver(self, client, monkeypatch):
        _patch_http(monkeypatch, _redirect_handler(_mod_routes()))
        resp = client.post("/api/curseforge/download",
                           json={"project_id": "cfmod"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["filename"] == "cfmod-old.jar"
        assert data["target"]["type"] == "main"
        job_id = data["job_id"]
        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert job["status"] == "done", job.get("error")
        assert (settings.mods_dir / "cfmod-old.jar").read_bytes() == JAR
        assert job["source"] == "curseforge"

    def test_download_instanz_ziel(self, client, monkeypatch):
        inst = instances.create_instance("CfModInst", "forge", "1.21.4",
                                         accept_eula=True)
        _patch_http(monkeypatch, _redirect_handler(_mod_routes()))
        resp = client.post("/api/curseforge/download",
                           json={"project_id": "cfmod",
                                 "instance_id": inst["id"]})
        assert resp.status_code == 200, resp.text
        assert resp.json()["target"] == {"type": "instance",
                                         "instance_id": inst["id"]}

    def test_409_bei_vorhandener_datei(self, client, monkeypatch):
        settings.mods_dir.mkdir(parents=True, exist_ok=True)
        (settings.mods_dir / "cfmod-old.jar").write_bytes(b"alt")
        _patch_http(monkeypatch, _redirect_handler(_mod_routes()))
        resp = client.post("/api/curseforge/download",
                           json={"project_id": "cfmod"})
        assert resp.status_code == 409
        assert resp.json()["detail"].startswith("'cfmod-old.jar'")

    def test_unbekannte_instanz_404(self, client):
        resp = client.post("/api/curseforge/download",
                           json={"project_id": "cfmod",
                                 "instance_id": "gibtsnicht"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Pack-Installation via CF-API
# ---------------------------------------------------------------------------

_CF_MANIFEST = {
    "name": "CF API Pack",
    "minecraft": {
        "version": "1.21.4",
        "modLoaders": [{"id": "fabric-0.16.9", "primary": True}],
    },
    "files": [{"projectID": 111, "fileID": 222, "required": True}],
}


def _cf_zip() -> bytes:
    """Deterministisches CF-Pack-Zip: feste ZipInfo-Timestamps UND ein frisches
    Manifest pro Aufruf (die Pipeline mutiert die File-Dicts beim Normalisieren)."""
    manifest = {
        "name": "CF API Pack",
        "minecraft": {
            "version": "1.21.4",
            "modLoaders": [{"id": "fabric-0.16.9", "primary": True}],
        },
        "files": [{"projectID": 111, "fileID": 222, "required": True}],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in (("manifest.json", json.dumps(manifest)),
                              ("overrides/config/app.cfg", CFG)):
            info = zipfile.ZipInfo(name, date_time=(2025, 1, 1, 0, 0, 0))
            z.writestr(info, content)
    return buf.getvalue()


def _pack_routes():
    pack = _cf_zip()
    return {
        "/mods/search": (200, {"data": [_mod(4471)], "pagination": {}}),
        "/mods/123/files": (200, {"data": [_file(700, "CFPack.zip",
                                                 "2025-01-01T00:00:00Z",
                                                 len(pack),
                                                 sha1=hashlib.sha1(pack).hexdigest())],
                                  "pagination": {}}),
        "/CFPack.zip": (200, pack),
        "/mods/111/files/222/download": (302, b""),
        "/testmod.jar": (200, JAR),
    }


def _pack_redirect_handler(routes):
    return _redirect_handler(routes)


class TestPackInstallCf:
    def test_end_to_end(self, monkeypatch):
        inst = instances.create_instance("CfPack", "fabric", "1.21.4",
                                         accept_eula=True)
        _patch_http(monkeypatch, _pack_redirect_handler(_pack_routes()))
        job = _await_job(packs.install_pack_cf(inst["id"], "cfmod"))
        assert job["status"] == "done", job["error"]
        assert job["source"] == "curseforge"
        root = instances.instance_dir(inst["id"])
        # Mod über Manifest-Download (Redirect-Dateiname)
        assert (root / "mods" / "testmod.jar").read_bytes() == JAR
        # Override aus dem Pack extrahiert
        assert (root / "config" / "app.cfg").read_bytes() == CFG
        meta = instances.get_instance(inst["id"])
        assert meta["modpack"]["source"] == "curseforge"
        assert meta["modpack"]["project_id"] == "cfmod"
        assert meta["modpack"]["title"] == "CF API Pack"
        assert meta["modpack"]["format"] == "curseforge"
        assert meta["loader_version"] == "0.16.9"

    def test_route_startet_job(self, client, monkeypatch):
        """Route dispatcht an install_pack_cf. Der Hintergrund-Job läuft außerhalb
        der Anfrage — der TestClient beendet BG-Tasks nach der Antwort (Testumgebung),
        der Endzustand ist über die Direktaufruf-Tests abgedeckt."""
        inst = instances.create_instance("CfPackApi", "fabric", "1.21.4",
                                         accept_eula=True)
        _patch_http(monkeypatch, _pack_redirect_handler(_pack_routes()))
        resp = client.post(f"/api/instances/{inst['id']}/modpacks/install",
                           json={"project_id": "cfmod", "source": "curseforge"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["filename"] == "CFPack.zip"
        job = client.get(f"/api/jobs/{data['job_id']}").json()
        assert job["kind"] == "pack"
        assert job["source"] == "curseforge"
        assert job["project_id"] == "cfmod"

    def test_file_id_wird_verwendet(self, monkeypatch):
        inst = instances.create_instance("CfPackFid", "fabric", "1.21.4",
                                         accept_eula=True)
        pack = _cf_zip()
        routes = _pack_routes()
        routes["/mods/123/files"] = (200, {"data": [
            _file(700, "CFPack.zip", "2025-01-01T00:00:00Z", len(pack),
                  sha1=hashlib.sha1(pack).hexdigest()),
            _file(701, "CFPack-old.zip", "2024-01-01T00:00:00Z", len(pack),
                  sha1=hashlib.sha1(pack).hexdigest()),
        ], "pagination": {}})
        routes["/CFPack-old.zip"] = (200, pack)
        _patch_http(monkeypatch, _pack_redirect_handler(routes))

        job = _await_job(packs.install_pack_cf(inst["id"], "cfmod", file_id="701"))
        assert job["status"] == "done", job["error"]
        assert job["filename"] == "CFPack-old.zip"

    def test_konflikt_409(self):
        inst = instances.create_instance("CfKonflikt", "fabric", "1.21.4",
                                         accept_eula=True)
        inst["modpack"] = {"title": "Altes Pack"}
        instances.update_instance(inst)
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.install_pack_cf(inst["id"], "cfmod"))
        assert e.value.status_code == 409

    def test_laufende_installation_409(self):
        inst = instances.create_instance("CfBusy", "fabric", "1.21.4",
                                         accept_eula=True)
        modrinth.create_job("x.zip", 100, kind="pack", instance_id=inst["id"])
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.install_pack_cf(inst["id"], "cfmod"))
        assert e.value.status_code == 409

    def test_unbekannte_instanz_404(self):
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.install_pack_cf("gibtsnicht", "cfmod"))
        assert e.value.status_code == 404

    def test_zu_grosses_pack_413(self, monkeypatch):
        inst = instances.create_instance("CfRiesig", "fabric", "1.21.4",
                                         accept_eula=True)
        big = _file(700, "CFPack.zip", "2025-01-01T00:00:00Z",
                    packs._MAX_PACK_BYTES + 1)
        _patch_http(monkeypatch, _handler({
            "/mods/search": (200, {"data": [_mod(4471)], "pagination": {}}),
            "/mods/123/files": (200, {"data": [big], "pagination": {}}),
        }))
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.install_pack_cf(inst["id"], "cfmod"))
        assert e.value.status_code == 413

    def test_zu_wenig_speicher_507(self, monkeypatch):
        inst = instances.create_instance("CfVoll", "fabric", "1.21.4",
                                         accept_eula=True)
        routes = _pack_routes()
        routes["/CFPack.zip"] = (200, b"")  # wird nie geladen
        _patch_http(monkeypatch, _handler(routes))
        usage = collections.namedtuple("usage", "total used free")
        monkeypatch.setattr(shutil, "disk_usage",
                            lambda target: usage(total=10**9, used=0, free=10**6))
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.install_pack_cf(inst["id"], "cfmod"))
        assert e.value.status_code == 507

    def test_inkompatibles_manifest_fehler(self, monkeypatch):
        """Pack für falsche MC-Version → Job-Fehler, Zip aufgeräumt."""
        inst = instances.create_instance("CfInkomp", "fabric", "1.20.1",
                                         accept_eula=True)
        _patch_http(monkeypatch, _pack_redirect_handler(_pack_routes()))
        job = _await_job(packs.install_pack_cf(inst["id"], "cfmod"))
        assert job["status"] == "error"
        assert "1.21.4" in job["error"] and "1.20.1" in job["error"]
        assert not list(instances.pack_dir(inst["id"]).glob("*.zip"))

    def test_sha1_mismatch_fehler(self, monkeypatch):
        """Manipuliertes Pack-Zip → SHA1-Prüfung schlägt fehl, Zip aufgeräumt."""
        inst = instances.create_instance("CfHash", "fabric", "1.21.4",
                                         accept_eula=True)
        routes = _pack_routes()
        routes["/mods/123/files"] = (200, {"data": [
            _file(700, "CFPack.zip", "2025-01-01T00:00:00Z", len(_cf_zip())),
            # sha1-Default = JAR-Hash ≠ Pack-Hash → Mismatch
        ], "pagination": {}})
        _patch_http(monkeypatch, _pack_redirect_handler(routes))
        job = _await_job(packs.install_pack_cf(inst["id"], "cfmod"))
        assert job["status"] == "error"
        assert "SHA1" in job["error"]
        assert not list(instances.pack_dir(inst["id"]).glob("*.zip"))

    def test_zip_download_fehler(self, monkeypatch):
        inst = instances.create_instance("CfDownErr", "fabric", "1.21.4",
                                         accept_eula=True)
        routes = _pack_routes()
        routes["/CFPack.zip"] = (500, b"nope")
        _patch_http(monkeypatch, _pack_redirect_handler(routes))
        job = _await_job(packs.install_pack_cf(inst["id"], "cfmod"))
        assert job["status"] == "error"
        assert "500" in job["error"]
        assert not list(instances.pack_dir(inst["id"]).glob("*.zip"))

    def test_mod_download_fehlgeschlagen(self, monkeypatch):
        inst = instances.create_instance("CfModErr", "fabric", "1.21.4",
                                         accept_eula=True)
        routes = _pack_routes()
        routes["/testmod.jar"] = (500, b"nope")
        _patch_http(monkeypatch, _pack_redirect_handler(routes))
        job = _await_job(packs.install_pack_cf(inst["id"], "cfmod"))
        assert job["status"] == "error"
        assert "fehlgeschlagen" in job["error"]
        assert instances.get_instance(inst["id"])["modpack"] is None


# ---------------------------------------------------------------------------
# Upload-Pipeline bleibt unverändert (source-Default)
# ---------------------------------------------------------------------------

class TestUploadSourceDefault:
    def test_upload_hat_source_upload(self, monkeypatch, tmp_path):
        inst = instances.create_instance("SrcUpload", "fabric", "1.21.4",
                                         accept_eula=True)
        _patch_http(monkeypatch, _redirect_handler(_mod_routes()))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("modrinth.index.json", json.dumps({
                "formatVersion": 1, "gameVersion": "1.21.4",
                "dependencies": {"fabric-loader": "0.16.9"},
                "files": [{"path": "mods/testmod.jar",
                           "downloads": ["https://cdn/testmod.jar"],
                           "fileSize": len(JAR),
                           "hashes": {"sha1": hashlib.sha1(JAR).hexdigest()}}],
            }))
        dest = instances.pack_dir(inst["id"]) / "up.mrpack"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(buf.getvalue())

        job = _await_job(packs.install_upload(inst["id"], dest, dest.name))
        assert job["status"] == "done", job["error"]
        meta = instances.get_instance(inst["id"])
        assert meta["modpack"]["source"] == "upload"
        assert meta["modpack"]["project_id"] is None
