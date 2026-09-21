"""Tests für 'Server direkt aus Modpack erstellen' (app.packs.create_server_*
+ Routen /api/modpacks/search, /api/instances/from-pack[-upload]).

Abgedeckt: Modrinth- und CurseForge-Ablauf (inkl. Server-Pack-Datei),
Namens-/EULA-/Loader-Validierung, Cleanup bei Fehlern, Upload-Variante und
der serverseitige env-Filter für nur-Client-Dateien.
"""
import asyncio
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
    """Saubere Instanz-/Job-Umgebung + CF-API-Key."""
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
    monkeypatch.setattr(curseforge, "_new_client", factory)


def _handler(routes):
    """routes: {path_suffix: (status, content)} — bytes oder dict/list → json."""

    def handle(request: httpx.Request) -> httpx.Response:
        for suffix, (status, content) in routes.items():
            if request.url.path.endswith(suffix):
                if isinstance(content, (dict, list)):
                    return httpx.Response(status, json=content)
                return httpx.Response(status, content=content)
        return httpx.Response(404, json={"errorCode": 404})

    return handle


JAR = b"frompack-fake-jar" * 10
CFG = b"frompack-config=1\n"


# ---------------------------------------------------------------------------
# Modrinth
# ---------------------------------------------------------------------------

def _mrpack(name="MR Pack", files=True) -> bytes:
    index = {
        "formatVersion": 1,
        "gameVersion": "1.21.4",
        "name": name,
        "dependencies": {"fabric-loader": "0.16.9"},
        "files": [
            {"path": "mods/testmod.jar",
             "downloads": ["https://cdn.example/testmod.jar"],
             "fileSize": len(JAR), "hashes": {"sha1": hashlib.sha1(JAR).hexdigest()}},
            {"path": "config/app.cfg",
             "downloads": ["https://cdn.example/app.cfg"],
             "fileSize": len(CFG), "hashes": {"sha1": hashlib.sha1(CFG).hexdigest()}},
        ] if files else [],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("modrinth.index.json", json.dumps(index))
    return buf.getvalue()


def _version(pack: bytes, version_id="v1", name="MR Pack", loaders=None,
             game_versions=None, size=None):
    return {
        "id": version_id,
        "project_id": "packproj",
        "name": name,
        "loaders": ["fabric"] if loaders is None else loaders,
        "game_versions": ["1.21.4"] if game_versions is None else game_versions,
        "files": [{"filename": f"{name}.mrpack",
                   "url": "https://cdn.example/pack.mrpack",
                   "size": size if size is not None else len(pack),
                   "primary": True}],
    }


def _modrinth_routes(pack, version=None, jar=JAR, cfg=CFG):
    version = version or _version(pack)
    return {
        "/project/packproj/version": (200, json.dumps([version]).encode("utf-8")),
        "/version/v1": (200, json.dumps(version).encode("utf-8")),
        "/pack.mrpack": (200, pack),
        "/testmod.jar": (200, jar),
        "/app.cfg": (200, cfg),
    }


async def _run_until_done(job, limit=500):
    for _ in range(limit):
        if job["status"] in ("done", "error"):
            return job
        await asyncio.sleep(0.02)
    return job


class TestCreateFromModrinth:
    def test_end_to_end(self, monkeypatch):
        pack = _mrpack()
        _patch_http(monkeypatch, _handler(_modrinth_routes(pack)))

        async def flow():
            result = await packs.create_server_from_pack(
                project_id="packproj", accept_eula=True)
            await _run_until_done(result["job"])
            return result

        result = asyncio.run(flow())
        inst, job = result["instance"], result["job"]
        assert job["status"] == "done", job["error"]
        assert inst["loader"] == "fabric"
        assert inst["game_version"] == "1.21.4"
        assert inst["name"] == "MR Pack"
        root = instances.instance_dir(inst["id"])
        assert (root / "mods" / "testmod.jar").read_bytes() == JAR
        assert (root / "config" / "app.cfg").read_bytes() == CFG
        meta = instances.get_instance(inst["id"])
        assert meta["modpack"]["title"] == "MR Pack"
        assert meta["loader_version"] == "0.16.9"

    def test_namen_konflikt_erhaelt_suffix(self, monkeypatch):
        instances.create_instance("MR Pack", "fabric", "1.21.4", accept_eula=True)
        pack = _mrpack()
        _patch_http(monkeypatch, _handler(_modrinth_routes(pack)))

        async def flow():
            result = await packs.create_server_from_pack(
                project_id="packproj", accept_eula=True)
            await _run_until_done(result["job"])
            return result

        result = asyncio.run(flow())
        assert result["instance"]["name"] == "MR Pack 2"

    def test_sonderzeichen_im_titel(self, monkeypatch):
        pack = _mrpack(name="Better MC [1.21.4]")
        version = _version(pack, name="Better MC [1.21.4]")
        _patch_http(monkeypatch, _handler(_modrinth_routes(pack, version=version)))

        async def flow():
            result = await packs.create_server_from_pack(
                project_id="packproj", accept_eula=True)
            await _run_until_done(result["job"])
            return result

        result = asyncio.run(flow())
        assert result["instance"]["name"] == "Better MC 1.21.4"

    def test_eula_fehlt_400(self, monkeypatch):
        pack = _mrpack(files=False)
        _patch_http(monkeypatch, _handler(_modrinth_routes(pack)))
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.create_server_from_pack(project_id="packproj"))
        assert e.value.status_code == 400
        assert instances.list_instances() == []

    def test_unbekannter_loader_400(self, monkeypatch):
        pack = _mrpack()
        version = _version(pack, loaders=["bukkit"])
        _patch_http(monkeypatch, _handler(_modrinth_routes(pack, version=version)))
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.create_server_from_pack(project_id="packproj",
                                                      accept_eula=True))
        assert e.value.status_code == 400
        assert "Loader" in e.value.detail
        assert instances.list_instances() == []

    def test_keine_mc_version_400(self, monkeypatch):
        pack = _mrpack()
        version = _version(pack, game_versions=[])
        routes = _modrinth_routes(pack, version=version)
        routes["/pack.mrpack"] = (200, b"")
        _patch_http(monkeypatch, _handler(routes))
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.create_server_from_pack(project_id="packproj",
                                                      accept_eula=True))
        assert e.value.status_code == 400
        assert "Minecraft-Version" in e.value.detail

    def test_cleanup_bei_installationsstart_fehler(self, monkeypatch):
        pack = _mrpack(files=False)
        _patch_http(monkeypatch, _handler(_modrinth_routes(pack)))

        async def boom(*args, **kwargs):
            raise HTTPException(status_code=502, detail="boom")

        monkeypatch.setattr(packs, "install_pack", boom)
        with pytest.raises(HTTPException):
            asyncio.run(packs.create_server_from_pack(project_id="packproj",
                                                      accept_eula=True))
        assert instances.list_instances() == []


class TestEnvServerFilter:
    def test_nur_client_dateien_werden_uebersprungen(self, tmp_path):
        index = {"files": [
            {"path": "mods/server.jar", "downloads": ["https://x/s.jar"],
             "fileSize": 1, "env": {"client": "required", "server": "required"}},
            {"path": "mods/client.jar", "downloads": ["https://x/c.jar"],
             "fileSize": 1, "env": {"client": "required", "server": "unsupported"}},
            {"path": "mods/default.jar", "downloads": ["https://x/d.jar"],
             "fileSize": 1},  # ohne env → wird installiert
        ]}
        plan, skipped = packs._file_plan(tmp_path, index)
        assert [d.name for _, d, _, _ in plan] == ["server.jar", "default.jar"]
        assert len(skipped) == 1
        assert "nur Client" in skipped[0]


# ---------------------------------------------------------------------------
# CurseForge inkl. Server-Pack-Datei
# ---------------------------------------------------------------------------

def _cf_manifest() -> dict:
    return {
        "name": "CF Server Pack",
        "minecraft": {
            "version": "1.21.4",
            "modLoaders": [{"id": "fabric-0.16.9", "primary": True}],
        },
        "files": [{"projectID": 111, "fileID": 222, "required": True}],
    }


def _cf_zip() -> bytes:
    """Deterministisches Pack-Zip (feste Zip-Timestamps)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in (
                ("manifest.json", json.dumps(_cf_manifest())),
                ("overrides/config/app.cfg", CFG)):
            z.writestr(zipfile.ZipInfo(name, date_time=(2025, 1, 1, 0, 0, 0)),
                       content)
    return buf.getvalue()


def _cf_mod():
    return {"id": 123, "name": "CF Pack", "slug": "cfmod", "classId": 4471,
            "summary": "s", "downloadCount": 1, "dateModified": "2025-01-01",
            "logo": {}, "authors": []}


def _cf_client_file(pack):
    return {
        "id": 700, "fileName": "CFPack.zip",
        "fileDate": "2025-06-01T00:00:00Z", "fileSize": len(pack),
        "releaseType": 1, "gameVersions": ["1.21.4", "Fabric"],
        "serverPackFileId": 701, "downloadUrl": None,
        "hashes": {"sha1": hashlib.sha1(pack).hexdigest()},
    }


def _cf_server_file(pack):
    return {
        "id": 701, "fileName": "CFPack-server.zip",
        "fileDate": "2025-01-01T00:00:00Z", "fileSize": len(pack),
        "releaseType": 1, "gameVersions": ["1.21.4"],
        "downloadUrl": None,
        "hashes": {"sha1": hashlib.sha1(pack).hexdigest()},
    }


def _cf_routes(pack, client_file=None, server_file=None):
    routes = {
        "/mods/search": (200, {"data": [_cf_mod()], "pagination": {}}),
        "/mods/123/files": (200, {
            "data": [f for f in (client_file, server_file) if f],
            "pagination": {}}),
        "/CFPack-server.zip": (200, pack),
        "/CFPack.zip": (200, pack),
        "/mods/111/files/222/download": (302, b""),
        "/testmod.jar": (200, JAR),
    }
    return routes


def _cf_redirect_handler(routes):
    base = _handler(routes)
    redirects = {
        "/mods/123/files/700/download": "CFPack.zip",
        "/mods/123/files/701/download": "CFPack-server.zip",
        "/mods/111/files/222/download": "testmod.jar",
    }

    def handle(request: httpx.Request) -> httpx.Response:
        for suffix, name in redirects.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(302, headers={
                    "Location": f"https://mediafilez.forgecdn.net/files/9/{name}"})
        return base(request)

    return handle


class TestCreateFromCurseForge:
    def test_server_pack_wird_verwendet(self, monkeypatch):
        pack = _cf_zip()
        routes = _cf_routes(pack, client_file=_cf_client_file(pack),
                            server_file=_cf_server_file(pack))
        _patch_http(monkeypatch, _cf_redirect_handler(routes))

        async def flow():
            result = await packs.create_server_from_pack(
                project_id="cfmod", source="curseforge", accept_eula=True)
            await _run_until_done(result["job"])
            return result

        result = asyncio.run(flow())
        inst, job = result["instance"], result["job"]
        assert job["status"] == "done", job["error"]
        assert inst["loader"] == "fabric"
        assert inst["game_version"] == "1.21.4"
        assert inst["name"] == "CF Pack"
        assert job["filename"] == "CFPack-server.zip"
        root = instances.instance_dir(inst["id"])
        assert (root / "mods" / "testmod.jar").read_bytes() == JAR
        assert (root / "config" / "app.cfg").read_bytes() == CFG
        meta = instances.get_instance(inst["id"])
        assert meta["modpack"]["loader_version"] == "0.16.9"

    def test_ohne_server_pack_faellt_auf_haupt_datei(self, monkeypatch):
        pack = _cf_zip()
        client_file = _cf_client_file(pack)
        del client_file["serverPackFileId"]
        routes = _cf_routes(pack, client_file=client_file)
        _patch_http(monkeypatch, _cf_redirect_handler(routes))

        async def flow():
            result = await packs.create_server_from_pack(
                project_id="cfmod", source="curseforge", accept_eula=True)
            await _run_until_done(result["job"])
            return result

        result = asyncio.run(flow())
        assert result["job"]["status"] == "done", result["job"]["error"]
        assert result["job"]["filename"] == "CFPack.zip"

    def test_loader_nicht_ableitbar_400(self, monkeypatch):
        pack = _cf_zip()
        client_file = _cf_client_file(pack)
        client_file["gameVersions"] = ["1.21.4"]  # kein Loader-Tag
        client_file["serverPackFileId"] = 701
        routes = _cf_routes(pack, client_file=client_file,
                            server_file=_cf_server_file(pack))
        routes["/CFPack-server.zip"] = (200, b"")  # wird nie geladen
        _patch_http(monkeypatch, _cf_redirect_handler(routes))
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.create_server_from_pack(
                project_id="cfmod", source="curseforge", accept_eula=True))
        assert e.value.status_code == 400
        assert "Loader" in e.value.detail
        assert instances.list_instances() == []

    def test_speicherplatz_zu_klein_507(self, monkeypatch):
        import collections
        pack = _cf_zip()
        routes = _cf_routes(pack, client_file=_cf_client_file(pack),
                            server_file=_cf_server_file(pack))
        routes["/CFPack-server.zip"] = (200, b"")
        _patch_http(monkeypatch, _cf_redirect_handler(routes))
        usage = collections.namedtuple("usage", "total used free")
        monkeypatch.setattr(shutil, "disk_usage",
                            lambda target: usage(total=10**9, used=0, free=10**6))
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.create_server_from_pack(
                project_id="cfmod", source="curseforge", accept_eula=True))
        assert e.value.status_code == 507
        assert instances.list_instances() == []


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------

class TestGlobalSearchRoute:
    def test_modrinth_global(self, client, monkeypatch):
        payload = {"total": 1, "hits": [
            {"project_id": "p1", "slug": "p", "title": "T", "description": "",
             "downloads": 1, "icon_url": "", "loaders": ["fabric"],
             "versions": ["1.21.4"]}]}
        _patch_http(monkeypatch, _handler({
            "/search": (200, json.dumps(payload).encode("utf-8"))}))
        resp = client.get("/api/modpacks/search", params={"q": "pack"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "modrinth"
        assert data["hits"][0]["compatible"] is None
        assert "instance" not in data

    def test_curseforge_global_leitet_loader_ab(self, client, monkeypatch):
        mod = _cf_mod()
        mod["latestFilesIndexes"] = [
            {"gameVersion": "1.21.4", "modloader": "Fabric"},
            {"gameVersion": "1.20.1", "modloader": "Forge"},
        ]
        _patch_http(monkeypatch, _handler({
            "/mods/search": (200, {"data": [mod],
                                   "pagination": {"totalCount": 1}})}))
        resp = client.get("/api/modpacks/search",
                          params={"q": "pack", "source": "curseforge"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "curseforge"
        hit = data["hits"][0]
        assert hit["loaders"] == ["fabric", "forge"]
        assert hit["versions"] == ["1.20.1", "1.21.4"]


# ---------------------------------------------------------------------------
# Filter + Versionsliste
# ---------------------------------------------------------------------------

class TestSearchFilters:
    def test_modrinth_facets_enthalten_filter(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["facets"] = json.loads(request.url.params["facets"])
            return httpx.Response(200, json={"total": 0, "hits": []})

        _patch_http(monkeypatch, handler)
        asyncio.run(packs.search_modpacks_global("pack", loader="forge",
                                                 game_version="1.20.1"))
        flat = [item for group in captured["facets"] for item in group]
        assert "project_type:modpack" in flat
        assert "versions:1.20.1" in flat
        assert "categories:forge" in flat

    def test_modrinth_ohne_filter_nur_typ(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["facets"] = json.loads(request.url.params["facets"])
            return httpx.Response(200, json={"total": 0, "hits": []})

        _patch_http(monkeypatch, handler)
        asyncio.run(packs.search_modpacks_global("pack"))
        flat = [item for group in captured["facets"] for item in group]
        assert flat == ["project_type:modpack"]

    def test_curseforge_filter_parameter(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"data": [], "pagination": {}})

        _patch_http(monkeypatch, handler)
        asyncio.run(curseforge.search_modpacks_global("pack", loader="forge",
                                                      game_version="1.20.1"))
        p = captured["params"]
        assert p["gameVersion"] == "1.20.1"
        assert p["modLoaderType"] == "1"  # forge
        assert p["classId"] == "4471"

    def test_curseforge_unbekannter_loader_400(self, monkeypatch):
        _patch_http(monkeypatch, _handler({"/mods/search": (200, b"[]")}))
        with pytest.raises(HTTPException) as e:
            asyncio.run(curseforge.search_modpacks_global("x", loader="paper"))
        assert e.value.status_code == 400
        assert "paper" in e.value.detail

    def test_route_filter_modrinth(self, client, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["facets"] = json.loads(request.url.params["facets"])
            return httpx.Response(200, json={"total": 0, "hits": []})

        _patch_http(monkeypatch, handler)
        resp = client.get("/api/modpacks/search",
                          params={"loader": "fabric", "game_version": "1.21.4"})
        assert resp.status_code == 200
        flat = [item for group in captured["facets"] for item in group]
        assert "categories:fabric" in flat
        assert "versions:1.21.4" in flat

    def test_route_unbekannter_loader_400(self, client, monkeypatch):
        _patch_http(monkeypatch, _handler({"/mods/search": (200, b"[]")}))
        resp = client.get("/api/modpacks/search",
                          params={"loader": "paper", "source": "curseforge"})
        assert resp.status_code == 400


class TestPackVersions:
    def test_modrinth(self, monkeypatch):
        _pack = _mrpack(files=False)
        file_entry = {"filename": "x.mrpack", "url": "https://cdn.example/pack.mrpack",
                      "size": 10, "primary": True}

        def version(vid, name, gvs, loaders, date, featured):
            return {"id": vid, "project_id": "packproj", "name": name,
                    "loaders": loaders, "game_versions": gvs,
                    "files": [file_entry], "date_published": date,
                    "featured": featured}

        payload = [
            version("v1", "MR Pack 1.21", ["1.21.4"], ["fabric"],
                    "2025-06-01T00:00:00Z", True),
            version("v2", "MR Pack 1.20", ["1.20.1"], ["forge"],
                    "2024-01-01T00:00:00Z", False),
        ]
        _patch_http(monkeypatch, _handler({
            "/project/packproj/version": (200, json.dumps(payload).encode("utf-8"))}))
        result = asyncio.run(packs.list_pack_versions("modrinth", "packproj"))
        assert result["source"] == "modrinth"
        assert len(result["versions"]) == 2
        first = result["versions"][0]
        assert first["version_id"] == "v1"
        assert first["game_versions"] == ["1.21.4"]
        assert first["loaders"] == ["fabric"]
        assert first["featured"] is True
        assert first["size"] == 10

    def test_curseforge(self, monkeypatch):
        pack = _cf_zip()
        routes = _cf_routes(pack, client_file=_cf_client_file(pack),
                            server_file=_cf_server_file(pack))
        _patch_http(monkeypatch, _cf_redirect_handler(routes))
        result = asyncio.run(packs.list_pack_versions("curseforge", "cfmod"))
        assert result["source"] == "curseforge"
        assert len(result["versions"]) == 2
        # Neueste zuerst (Haupt-Datei 2025-06 vor Server-Pack 2025-01)
        assert result["versions"][0]["file_id"] == "700"
        top = result["versions"][0]
        assert top["name"] == "CFPack.zip"
        assert top["loaders"] == ["fabric"]
        assert top["game_versions"] == ["1.21.4"]
        assert top["server_pack"] is True
        assert top["release_label"] == "Stabil"

    def test_route_modrinth(self, client, monkeypatch):
        payload = [{"id": "v1", "project_id": "packproj", "name": "n",
                    "loaders": ["fabric"], "game_versions": ["1.21.4"],
                    "files": [], "date_published": "", "featured": False}]
        _patch_http(monkeypatch, _handler({
            "/project/packproj/version": (200, json.dumps(payload).encode("utf-8"))}))
        resp = client.get("/api/modpacks/versions",
                          params={"project_id": "packproj"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["versions"][0]["version_id"] == "v1"

    def test_route_curseforge(self, client, monkeypatch):
        pack = _cf_zip()
        routes = _cf_routes(pack, client_file=_cf_client_file(pack))
        _patch_http(monkeypatch, _cf_redirect_handler(routes))
        resp = client.get("/api/modpacks/versions",
                          params={"project_id": "cfmod", "source": "curseforge"})
        assert resp.status_code == 200
        assert resp.json()["versions"][0]["file_id"] == "700"

    def test_route_unbekanntes_projekt_404(self, client, monkeypatch):
        _patch_http(monkeypatch, _handler({
            "/project/gibtsnicht/version": (404, b"gone")}))
        resp = client.get("/api/modpacks/versions",
                          params={"project_id": "gibtsnicht"})
        assert resp.status_code == 404


class TestFromPackVersionChoice:
    def test_modrinth_version_id_wird_verwendet(self, monkeypatch):
        pack = _mrpack()
        version = _version(pack, version_id="v2", name="MR Pack v2")
        routes = _modrinth_routes(pack, version=version)
        routes["/project/packproj/version"] = (200, json.dumps([version]).encode("utf-8"))
        routes["/version/v2"] = (200, json.dumps(version).encode("utf-8"))
        _patch_http(monkeypatch, _handler(routes))

        async def flow():
            result = await packs.create_server_from_pack(
                project_id="packproj", version_id="v2", accept_eula=True)
            await _run_until_done(result["job"])
            return result

        result = asyncio.run(flow())
        assert result["job"]["status"] == "done", result["job"]["error"]
        assert result["instance"]["name"] == "MR Pack v2"
        meta = instances.get_instance(result["instance"]["id"])
        assert meta["modpack"]["version_id"] == "v2"
        assert meta["modpack"]["title"] == "MR Pack v2"

    def test_curseforge_file_id_wird_verwendet(self, monkeypatch):
        pack = _cf_zip()
        routes = _cf_routes(pack, client_file=_cf_client_file(pack),
                            server_file=_cf_server_file(pack))
        _patch_http(monkeypatch, _cf_redirect_handler(routes))

        async def flow():
            result = await packs.create_server_from_pack(
                project_id="cfmod", source="curseforge", file_id="700",
                prefer_server_pack=False, accept_eula=True)
            await _run_until_done(result["job"])
            return result

        result = asyncio.run(flow())
        assert result["job"]["status"] == "done", result["job"]["error"]
        assert result["job"]["filename"] == "CFPack.zip"


class TestFromPackRoute:
    def test_erstellt_server(self, client, monkeypatch):
        pack = _mrpack()
        _patch_http(monkeypatch, _handler(_modrinth_routes(pack)))
        resp = client.post("/api/instances/from-pack",
                           json={"project_id": "packproj", "accept_eula": True})
        assert resp.status_code == 201, resp.text
        data = resp.json()
        inst, job = data["instance"], data["job"]
        assert inst["name"] == "MR Pack"
        assert inst["loader"] == "fabric"
        # Job über generischen Endpunkt abrufbar
        polled = client.get(f"/api/jobs/{job['id']}").json()
        assert polled["kind"] == "pack"
        for _ in range(100):
            polled = client.get(f"/api/jobs/{job['id']}").json()
            if polled["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert polled["status"] == "done", polled.get("error")

    def test_eula_fehlt_400(self, client, monkeypatch):
        pack = _mrpack(files=False)
        routes = _modrinth_routes(pack)
        routes["/pack.mrpack"] = (200, b"")
        _patch_http(monkeypatch, _handler(routes))
        resp = client.post("/api/instances/from-pack",
                           json={"project_id": "packproj"})
        assert resp.status_code == 400
        assert instances.list_instances() == []


class TestFromPackUploadRoute:
    def test_mrpack_erstellt_server(self, client, monkeypatch):
        _patch_http(monkeypatch, _handler({
            "/testmod.jar": (200, JAR),
            "/app.cfg": (200, CFG),
        }))
        resp = client.post(
            "/api/instances/from-pack-upload",
            data={"name": "Mein Pack", "accept_eula": "true"},
            files={"file": ("Mein Pack.mrpack", _mrpack(name="Mein Pack"),
                            "application/octet-stream")},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        inst = data["instance"]
        assert inst["name"] == "Mein Pack"
        assert inst["loader"] == "fabric"
        assert inst["game_version"] == "1.21.4"
        for _ in range(100):
            job = client.get(f"/api/jobs/{data['job']['id']}").json()
            if job["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert job["status"] == "done", job.get("error")
        root = instances.instance_dir(inst["id"])
        assert (root / "mods" / "testmod.jar").is_file()
        # Staging aufgeräumt (Archiv in die Instanz verschoben)
        assert not list(packs.staging_dir().glob("*.mrpack"))

    def test_cf_zip_erstellt_server(self, client, monkeypatch):
        _patch_http(monkeypatch, _cf_redirect_handler(
            _cf_routes(_cf_zip(), client_file=None)))
        resp = client.post(
            "/api/instances/from-pack-upload",
            data={"accept_eula": "true"},
            files={"file": ("CFPack.zip", _cf_zip(), "application/zip")},
        )
        assert resp.status_code == 201, resp.text
        inst = resp.json()["instance"]
        assert inst["loader"] == "fabric"
        assert inst["name"] == "CF Server Pack"

    def test_ungueltiges_archiv_400_und_cleanup(self, client):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("README.txt", "nix")
        resp = client.post(
            "/api/instances/from-pack-upload",
            data={"accept_eula": "true"},
            files={"file": ("bad.zip", buf.getvalue(), "application/zip")},
        )
        assert resp.status_code == 400
        assert instances.list_instances() == []
        assert not list(packs.staging_dir().glob("*.zip"))

    def test_ungueltiger_dateiname_400(self, client):
        resp = client.post(
            "/api/instances/from-pack-upload",
            data={"accept_eula": "true"},
            files={"file": ("../boese.zip", _cf_zip(), "application/zip")},
        )
        assert resp.status_code == 400
