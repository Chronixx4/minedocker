"""Tests für den Modpack-Installer (app.packs) mit vollständig gemocktem HTTP.

Abgedeckt: Suche mit Kompatibilitäts-Markierung, Download + SHA1-Prüfung,
Kompatibilitätsprüfung (MC-Version/Loader), Speicherplatz-Fehler (507),
Pfad-Schutz im mrpack, Job-Lifecycle inkl. Cleanup.
"""
import asyncio
import collections
import hashlib
import io
import json
import shutil
import tarfile
import time
import zipfile

import httpx
import pytest
from fastapi import HTTPException

from app import instances, modrinth, packs
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_umgebung():
    """Saubere Instanz- und Job-Umgebung pro Test."""
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
    """Ersetzt httpx.AsyncClient UND packs._new_client durch MockTransport."""
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        # Produktions-Client folgt Redirects (CurseForge-CDN) → hier nachbilden
        kwargs.setdefault("follow_redirects", True)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(packs, "_new_client", factory)


def _pack_handler(routes):
    """routes: {url_suffix: (status, bytes)} — Bytes-Antworten."""

    def handle(request: httpx.Request) -> httpx.Response:
        for suffix, (status, body) in routes.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(status, content=body)
        return httpx.Response(404, content=b"not found")

    return handle


JAR = b"fake-jar-inhalt" * 10
CFG = b"option=value\n"


def _mrpack(index: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("modrinth.index.json", json.dumps(index))
    return buf.getvalue()


def _index(game_version="1.21.4", loader_dep="fabric-loader", files=True):
    return {
        "formatVersion": 1,
        "gameVersion": game_version,
        "dependencies": {loader_dep: "0.16.9"} if loader_dep else {},
        "files": [
            {"path": "mods/testmod.jar",
             "downloads": ["https://cdn.example/testmod.jar"],
             "fileSize": len(JAR), "hashes": {"sha1": hashlib.sha1(JAR).hexdigest()}},
            {"path": "config/app.cfg",
             "downloads": ["https://cdn.example/app.cfg"],
             "fileSize": len(CFG), "hashes": {"sha1": hashlib.sha1(CFG).hexdigest()}},
            {"path": "saves/world.dat",
             "downloads": ["https://cdn.example/world.dat"],
             "fileSize": 8, "hashes": {"sha1": "0" * 40}},
        ] if files else [],
    }


def _version_payload(pack: bytes, project_id="packproj", version_id="v1",
                     name="Pack v1", size=None):
    return {
        "id": version_id,
        "project_id": project_id,
        "name": name,
        "files": [{"filename": f"{name}.mrpack",
                   "url": "https://cdn.example/pack.mrpack",
                   "size": size if size is not None else len(pack),
                   "primary": True}],
    }


def _full_routes(pack, version=None, jar=JAR, cfg=CFG):
    routes = {
        "/version/v1": (200, json.dumps(version or _version_payload(pack)).encode("utf-8")),
        "/pack.mrpack": (200, pack),
        "/testmod.jar": (200, jar),
        "/app.cfg": (200, cfg),
    }
    return routes


def _await_job(coro):
    """Führt die Installation aus und wartet im selben Eventloop auf das Job-Ende."""

    async def _flow():
        job = await coro
        for _ in range(500):
            if job["status"] in ("done", "error"):
                return job
            await asyncio.sleep(0.02)
        return job

    return asyncio.run(_flow())


class TestSearchModpacks:
    def test_kompatibilitaet_markierung(self, monkeypatch):
        inst = instances.create_instance("SearchSrv", "fabric", "1.21.4",
                                         accept_eula=True)
        payload = {"total": 2, "hits": [
            {"project_id": "p1", "slug": "fab-pack", "title": "Fab Pack",
             "description": "d", "downloads": 10, "icon_url": "",
             "loaders": ["fabric"], "versions": ["1.21.4", "1.20.1"]},
            {"project_id": "p2", "slug": "forge-pack", "title": "Forge Pack",
             "description": "d", "downloads": 5, "icon_url": "",
             "loaders": ["forge"], "versions": ["1.21.4"]},
        ]}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/search")
            facets = json.loads(request.url.params["facets"])
            flat = [item for group in facets for item in group]
            assert "project_type:modpack" in flat
            return httpx.Response(200, json=payload)

        _patch_http(monkeypatch, handler)
        result = asyncio.run(packs.search_modpacks(inst["id"], "pack"))
        assert result["hits"][0]["compatible"] is True
        assert result["hits"][1]["compatible"] is False
        assert result["instance"]["loader"] == "fabric"
        assert result["instance"]["game_version"] == "1.21.4"

    def test_unbekannte_instanz_404(self):
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.search_modpacks("gibtsnicht", "x"))
        assert e.value.status_code == 404

    def test_modrinth_fehler_502(self, monkeypatch):
        inst = instances.create_instance("SearchErr", "fabric", "1.21.4",
                                         accept_eula=True)
        _patch_http(monkeypatch, lambda r: httpx.Response(500))
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.search_modpacks(inst["id"], "x"))
        assert e.value.status_code == 502


class TestInstallErfolg:
    def test_vollstaendige_installation(self, monkeypatch):
        inst = instances.create_instance("PackSrv", "fabric", "1.21.4",
                                         accept_eula=True)
        pack = _mrpack(_index())
        _patch_http(monkeypatch, _pack_handler(_full_routes(pack)))

        job = _await_job(packs.install_pack(inst["id"], "packproj", version_id="v1"))
        assert job["status"] == "done", job["error"]
        assert job["kind"] == "pack"
        assert job["phase"] == "Fertig"

        root = instances.instance_dir(inst["id"])
        assert (root / "mods" / "testmod.jar").read_bytes() == JAR
        assert (root / "config" / "app.cfg").read_bytes() == CFG
        assert not list((root / "packs").glob("*.part"))
        assert (root / "packs" / "Pack v1.mrpack").is_file()

        meta = instances.get_instance(inst["id"])
        assert meta["modpack"]["title"] == "Pack v1"
        assert meta["modpack"]["loader_version"] == "0.16.9"
        assert meta["loader_version"] == "0.16.9"
        assert meta["modpack"]["files"] == 2
        assert job["summary"]["skipped"] == meta["modpack"]["skipped"]

    def test_automatische_versionswahl(self, monkeypatch):
        inst = instances.create_instance("AutoPack", "fabric", "1.20.1",
                                         accept_eula=True)
        pack = _mrpack(_index(game_version="1.20.1"))
        versions = [
            {"id": "v2", "loaders": ["forge"], "game_versions": ["1.20.1"],
             "files": []},
            dict(_version_payload(pack, version_id="v1", name="Auto Pack"),
                 loaders=["fabric"], game_versions=["1.20.1"]),
        ]
        routes = {
            "/project/packproj/version": (200, json.dumps(versions).encode("utf-8")),
            "/pack.mrpack": (200, pack),
            "/testmod.jar": (200, JAR),
            "/app.cfg": (200, CFG),
        }
        _patch_http(monkeypatch, _pack_handler(routes))

        job = _await_job(packs.install_pack(inst["id"], "packproj"))
        assert job["status"] == "done", job["error"]
        meta = instances.get_instance(inst["id"])
        assert meta["modpack"]["version_id"] == "v1"


class TestInstallFehler:
    def test_inkompatibler_loader(self, monkeypatch):
        inst = instances.create_instance("ForgeSrv", "forge", "1.20.1",
                                         accept_eula=True)
        pack = _mrpack(_index(game_version="1.20.1", loader_dep="fabric-loader"))
        _patch_http(monkeypatch, _pack_handler(_full_routes(pack)))

        job = _await_job(packs.install_pack(inst["id"], "packproj", version_id="v1"))
        assert job["status"] == "error"
        assert "Inkompatibel" in job["error"]
        assert "fabric" in job["error"]
        assert job["phase"] == "Fehlgeschlagen"
        # mrpack-Archiv wurde aufgeräumt
        assert not list(instances.pack_dir(inst["id"]).glob("*.mrpack"))

    def test_inkompatible_mc_version(self, monkeypatch):
        inst = instances.create_instance("AltSrv", "fabric", "1.20.1",
                                         accept_eula=True)
        pack = _mrpack(_index(game_version="1.21.4"))  # Pack braucht 1.21.4
        _patch_http(monkeypatch, _pack_handler(_full_routes(pack)))

        job = _await_job(packs.install_pack(inst["id"], "packproj", version_id="v1"))
        assert job["status"] == "error"
        assert "1.21.4" in job["error"] and "1.20.1" in job["error"]

    def test_mrpack_download_500(self, monkeypatch):
        inst = instances.create_instance("DownErr", "fabric", "1.21.4",
                                         accept_eula=True)
        pack = _mrpack(_index())
        routes = _full_routes(pack)
        routes["/pack.mrpack"] = (500, b"server error")
        _patch_http(monkeypatch, _pack_handler(routes))

        job = _await_job(packs.install_pack(inst["id"], "packproj", version_id="v1"))
        assert job["status"] == "error"
        assert "500" in job["error"]
        assert not list(instances.pack_dir(inst["id"]).glob("*.mrpack"))

    def test_mod_download_fehlgeschlagen(self, monkeypatch):
        inst = instances.create_instance("ModErr", "fabric", "1.21.4",
                                         accept_eula=True)
        pack = _mrpack(_index())
        routes = _full_routes(pack)
        routes["/testmod.jar"] = (500, b"nope")
        _patch_http(monkeypatch, _pack_handler(routes))

        job = _await_job(packs.install_pack(inst["id"], "packproj", version_id="v1"))
        assert job["status"] == "error"
        assert "fehlgeschlagen" in job["error"]
        assert "testmod.jar" in job["error"]
        # Andere Dateien dürfen vorhanden sein, aber kein fertiges "done"
        assert instances.get_instance(inst["id"])["modpack"] is None

    def test_sha1_mismatch(self, monkeypatch):
        inst = instances.create_instance("HashErr", "fabric", "1.21.4",
                                         accept_eula=True)
        pack = _mrpack(_index())
        routes = _full_routes(pack, jar=b"manipulierter inhalt")
        _patch_http(monkeypatch, _pack_handler(routes))

        job = _await_job(packs.install_pack(inst["id"], "packproj", version_id="v1"))
        assert job["status"] == "error"
        assert "SHA1" in job["error"]

    def test_konflikt_ohne_force(self):
        inst = instances.create_instance("Konflikt", "fabric", "1.21.4",
                                         accept_eula=True)
        inst["modpack"] = {"title": "Altes Pack"}
        instances.update_instance(inst)
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.install_pack(inst["id"], "packproj"))
        assert e.value.status_code == 409
        assert "force" in e.value.detail

    def test_konflikt_mit_force_installiert_neu(self, monkeypatch):
        inst = instances.create_instance("ForcePack", "fabric", "1.21.4",
                                         accept_eula=True)
        inst["modpack"] = {"title": "Altes Pack"}
        instances.update_instance(inst)
        pack = _mrpack(_index())
        _patch_http(monkeypatch, _pack_handler(_full_routes(pack)))

        job = _await_job(packs.install_pack(inst["id"], "packproj",
                                            version_id="v1", force=True))
        assert job["status"] == "done", job["error"]

    def test_laufende_installation_409(self, monkeypatch):
        inst = instances.create_instance("Busy", "fabric", "1.21.4",
                                         accept_eula=True)
        modrinth.create_job("x.mrpack", 100, kind="pack", instance_id=inst["id"])
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.install_pack(inst["id"], "packproj"))
        assert e.value.status_code == 409
        assert "läuft bereits" in e.value.detail

    def test_zu_groes_pack_413(self, monkeypatch):
        inst = instances.create_instance("Riesig", "fabric", "1.21.4",
                                         accept_eula=True)
        pack = _mrpack(_index())
        version = _version_payload(pack, size=packs._MAX_PACK_BYTES + 1)
        routes = _full_routes(pack, version=version)
        routes["/pack.mrpack"] = (200, b"")  # wird nie geladen
        _patch_http(monkeypatch, _pack_handler(routes))
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.install_pack(inst["id"], "packproj", version_id="v1"))
        assert e.value.status_code == 413

    def test_zu_wenig_speicher_507(self, monkeypatch):
        inst = instances.create_instance("Voll", "fabric", "1.21.4",
                                         accept_eula=True)
        pack = _mrpack(_index())
        version = _version_payload(pack)
        routes = _full_routes(pack, version=version)
        routes["/pack.mrpack"] = (200, b"")  # wird nie geladen
        _patch_http(monkeypatch, _pack_handler(routes))

        usage = collections.namedtuple("usage", "total used free")
        monkeypatch.setattr(shutil, "disk_usage",
                            lambda target: usage(total=10**9, used=0, free=10**6))
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.install_pack(inst["id"], "packproj", version_id="v1"))
        assert e.value.status_code == 507
        assert "Speicherplatz" in e.value.detail

    def test_version_id_fremdes_projekt_400(self, monkeypatch):
        inst = instances.create_instance("Fremd", "fabric", "1.21.4",
                                         accept_eula=True)
        pack = _mrpack(_index())
        version = _version_payload(pack, project_id="anderes-projekt")
        _patch_http(monkeypatch, _pack_handler(_full_routes(pack, version=version)))
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.install_pack(inst["id"], "packproj", version_id="v1"))
        assert e.value.status_code == 400

    def test_keine_kompatible_version_409(self, monkeypatch):
        inst = instances.create_instance("NixDa", "fabric", "1.20.1",
                                         accept_eula=True)
        pack = _mrpack(_index())
        versions = [dict(_version_payload(pack, version_id="v1"),
                         loaders=["forge"], game_versions=["1.21.4"])]
        routes = {
            "/project/packproj/version": (200, json.dumps(versions).encode("utf-8")),
        }
        _patch_http(monkeypatch, _pack_handler(routes))
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.install_pack(inst["id"], "packproj"))
        assert e.value.status_code == 409


class TestKompatibilitaetspruefung:
    def _inst(self, loader="fabric", gv="1.21.4"):
        return {"loader": loader, "game_version": gv}

    def _norm(self, **kw):
        return packs._normalize_index("modrinth", _index(**kw))

    def test_passend(self):
        dep = packs._check_compatibility(self._inst(), self._norm())
        assert dep == {"loader": "fabric", "version": "0.16.9",
                       "game_version": "1.21.4"}

    def test_neues_format_mc_in_dependencies(self):
        """Aktuelle mrpacks: gameVersion leer, MC-Version in dependencies.minecraft."""
        index = _index()
        index["gameVersion"] = None
        index["dependencies"]["minecraft"] = "1.21.4"
        pack = packs._normalize_index("modrinth", index)
        assert pack["game_version"] == "1.21.4"
        dep = packs._check_compatibility(self._inst(), pack)
        assert dep["game_version"] == "1.21.4"

    def test_curseforge_manifest_normalisierung(self):
        """CurseForge-manifest.json wird auf dasselbe Schema normalisiert."""
        manifest = {
            "name": "CF Pack",
            "minecraft": {
                "version": "1.21.4",
                "modLoaders": [{"id": "fabric-0.16.9", "primary": True}],
            },
            "files": [{"projectID": 111, "fileID": 222, "required": True}],
        }
        pack = packs._normalize_index("curseforge", manifest)
        assert pack["title"] == "CF Pack"
        assert pack["game_version"] == "1.21.4"
        assert pack["loader"] == "fabric"
        assert pack["loader_version"] == "0.16.9"
        assert pack["files"][0]["path"] is None
        assert "curseforge.com/api/v1/mods/111/files/222/download" in pack["files"][0]["downloads"][0]

    def test_falsche_mc_version(self):
        with pytest.raises(HTTPException) as e:
            packs._check_compatibility(self._inst(gv="1.20.1"), self._norm())
        assert e.value.status_code == 400
        assert "1.21.4" in e.value.detail

    def test_falscher_loader(self):
        with pytest.raises(HTTPException) as e:
            packs._check_compatibility(self._inst(loader="forge"), self._norm())
        assert e.value.status_code == 400
        assert "forge" in e.value.detail

    def test_paper_bekannter_hinweis(self):
        with pytest.raises(HTTPException) as e:
            packs._check_compatibility(self._inst(loader="paper"), self._norm())
        assert "Bukkit" in e.value.detail

    def test_kein_unterstuetzter_loader(self):
        with pytest.raises(HTTPException) as e:
            packs._check_compatibility(self._inst(), self._norm(loader_dep=None))
        assert e.value.status_code == 400

    def test_leerer_index(self):
        with pytest.raises(HTTPException) as e:
            packs._check_compatibility(self._inst(), self._norm(files=False))
        assert e.value.status_code == 400


class TestPfadSchutz:
    def test_file_plan_skippt_ungueltiges(self, tmp_path):
        index = {"files": [
            {"path": "../evil.jar", "downloads": ["https://x/y"], "fileSize": 1},
            {"path": "mods/ok.jar", "downloads": ["https://x/ok.jar"], "fileSize": 1},
            {"path": "saves/world.dat", "downloads": ["https://x/w"], "fileSize": 1},
            {"path": "nourl.jar", "downloads": ["http://insecure/x"], "fileSize": 1},
            {"path": "", "downloads": ["https://x/z"], "fileSize": 1},
        ]}
        plan, skipped = packs._file_plan(tmp_path, index)
        assert [d.name for _, d, _, _ in plan] == ["ok.jar"]
        assert len(skipped) == 4
        assert any("unsicherer Pfad" in s for s in skipped)
        assert any("Ordner nicht erlaubt" in s for s in skipped)

    def test_traversal_wirft_400(self, tmp_path):
        with pytest.raises(HTTPException) as e:
            packs._safe_pack_path(tmp_path, "../../etc/passwd")
        assert e.value.status_code == 400

    def test_erlaubte_ordner(self, tmp_path):
        for top in packs._ALLOWED_TOP_DIRS:
            assert packs._safe_pack_path(tmp_path, f"{top}/x") == tmp_path / top / "x"


class TestDownloadOne:
    def test_hash_mismatch_raeumt_auf(self, tmp_path):
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"falsch")))
        dest = tmp_path / "mods" / "x.jar"
        with pytest.raises(RuntimeError, match="SHA1"):
            asyncio.run(packs._download_one(client, "https://cdn/x.jar", dest,
                                            6, "0" * 40))
        assert not dest.exists()
        assert not dest.with_suffix(".jar.part").exists()

    def test_http_fehler(self, tmp_path):
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(403)))
        with pytest.raises(RuntimeError, match="403"):
            asyncio.run(packs._download_one(client, "https://cdn/x.jar",
                                            tmp_path / "x.jar", 0, None))

    def test_erfolgreicher_download_mit_hash(self, tmp_path):
        payload = b"mod-inhalt"
        sha1 = hashlib.sha1(payload).hexdigest()
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=payload)))
        dest = tmp_path / "mods" / "ok.jar"
        asyncio.run(packs._download_one(client, "https://cdn/ok.jar", dest,
                                        len(payload), sha1))
        assert dest.read_bytes() == payload


class TestExtractIndex:
    def test_beschädigtes_zip(self, tmp_path):
        bad = tmp_path / "bad.mrpack"
        bad.write_bytes(b"kein zip")
        with pytest.raises(HTTPException) as e:
            packs._extract_index(bad)
        assert e.value.status_code == 400

    def test_fehlender_index(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("README.txt", "nix")
        p = tmp_path / "noindex.mrpack"
        p.write_bytes(buf.getvalue())
        with pytest.raises(HTTPException) as e:
            packs._extract_index(p)
        assert e.value.status_code == 400


class TestPackApi:
    def test_install_route_startet_job(self, client, monkeypatch):
        inst = instances.create_instance("ApiPack", "fabric", "1.21.4",
                                         accept_eula=True)
        pack = _mrpack(_index())
        _patch_http(monkeypatch, _pack_handler(_full_routes(pack)))
        resp = client.post(f"/api/instances/{inst['id']}/modpacks/install",
                           json={"project_id": "packproj", "version_id": "v1"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["filename"] == "Pack v1.mrpack"
        # Generischer Job-Endpunkt liefert den Pack-Job
        job = client.get(f"/api/jobs/{data['job_id']}").json()
        assert job["kind"] == "pack"
        # Hintergrund-Job fertig warten (Status via API)
        for _ in range(100):
            job = client.get(f"/api/jobs/{data['job_id']}").json()
            if job["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert job["status"] == "done", job.get("error")

    def test_install_unbekannte_instanz_404(self, client):
        resp = client.post("/api/instances/gibtsnicht/modpacks/install",
                           json={"project_id": "p1"})
        assert resp.status_code == 404

    def test_search_route(self, client, monkeypatch):
        inst = instances.create_instance("ApiSearch", "fabric", "1.21.4",
                                         accept_eula=True)
        payload = {"total": 1, "hits": [
            {"project_id": "p1", "slug": "p", "title": "T", "description": "",
             "downloads": 1, "icon_url": "", "loaders": ["fabric"],
             "versions": ["1.21.4"]}]}
        _patch_http(monkeypatch, lambda r: httpx.Response(200, json=payload))
        resp = client.get(f"/api/instances/{inst['id']}/modpacks/search",
                          params={"q": "pack"})
        assert resp.status_code == 200
        assert resp.json()["hits"][0]["compatible"] is True


# ---------------------------------------------------------------------------
# Upload-Installation (.mrpack / CurseForge-.zip)
# ---------------------------------------------------------------------------

_CF_MANIFEST = {
    "name": "CF Pack",
    "minecraft": {
        "version": "1.21.4",
        "modLoaders": [{"id": "fabric-0.16.9", "primary": True}],
    },
    "files": [{"projectID": 111, "fileID": 222, "required": True}],
}


def _cf_zip(manifest=None, overrides=None) -> bytes:
    """CurseForge-Export-Simulation: manifest.json + overrides/-Ordner."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest or _CF_MANIFEST))
        for rel, content in (overrides or {}).items():
            z.writestr(f"overrides/{rel}", content)
    return buf.getvalue()


def _cf_routes():
    """CurseForge-CDN: Download-Endpoint redirectet zu forgecdn (Dateiname dort)."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/mods/111/files/222/download":
            return httpx.Response(302, headers={
                "Location": "https://mediafilez.forgecdn.net/files/2222/testmod.jar"})
        if request.url.path.endswith("/testmod.jar"):
            return httpx.Response(200, content=JAR)
        return httpx.Response(404, content=b"not found")

    return handle


class TestUploadInstall:
    """install_upload: hochgeladene Archive wie Modrinth-Packs installieren."""

    def test_mrpack_upload_end_to_end(self, monkeypatch):
        inst = instances.create_instance("UplMrpack", "fabric", "1.21.4",
                                         accept_eula=True)
        _patch_http(monkeypatch, _pack_handler({
            "/testmod.jar": (200, JAR),
            "/app.cfg": (200, CFG),
        }))
        dest = instances.pack_dir(inst["id"]) / "Mein Pack.mrpack"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_mrpack({**_index(), "name": "Mein Pack"}))

        job = _await_job(packs.install_upload(inst["id"], dest, dest.name))
        assert job["status"] == "done", job["error"]
        assert job["source"] == "upload"
        root = instances.instance_dir(inst["id"])
        assert (root / "mods" / "testmod.jar").read_bytes() == JAR
        assert (root / "config" / "app.cfg").read_bytes() == CFG
        meta = instances.get_instance(inst["id"])
        assert meta["modpack"]["source"] == "upload"
        assert meta["modpack"]["format"] == "modrinth"
        assert meta["modpack"]["title"] == "Mein Pack"

    def test_curseforge_upload_end_to_end(self, monkeypatch):
        inst = instances.create_instance("UplCF", "fabric", "1.21.4",
                                         accept_eula=True)
        _patch_http(monkeypatch, _cf_routes())
        overrides = {
            "config/app.cfg": CFG,          # erlaubt → wird extrahiert
            "saves/world.dat": b"w",        # Ordner nicht erlaubt → skipped
            "../evil.jar": b"e",            # Traversal → skipped
        }
        dest = instances.pack_dir(inst["id"]) / "CF Pack.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_cf_zip(overrides=overrides))

        job = _await_job(packs.install_upload(inst["id"], dest, dest.name))
        assert job["status"] == "done", job["error"]
        root = instances.instance_dir(inst["id"])
        # Mod über Redirect heruntergeladen, Dateiname aus CDN-URL
        assert (root / "mods" / "testmod.jar").read_bytes() == JAR
        assert (root / "config" / "app.cfg").read_bytes() == CFG
        assert not (root / "mods" / "world.dat").exists()
        assert not (root / "evil.jar").exists()
        meta = instances.get_instance(inst["id"])
        assert meta["modpack"]["source"] == "upload"
        assert meta["modpack"]["format"] == "curseforge"
        assert meta["loader_version"] == "0.16.9"
        assert any("Ordner nicht erlaubt" in s for s in meta["modpack"]["skipped"])
        assert any("unsicherer Pfad" in s for s in meta["modpack"]["skipped"])

    def test_upload_konflikt_409(self):
        inst = instances.create_instance("UplKonflikt", "fabric", "1.21.4",
                                         accept_eula=True)
        inst["modpack"] = {"title": "Altes Pack"}
        instances.update_instance(inst)
        dest = instances.pack_dir(inst["id"]) / "pack.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_cf_zip())
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.install_upload(inst["id"], dest, dest.name))
        assert e.value.status_code == 409

    def test_upload_laufende_installation_409(self):
        inst = instances.create_instance("UplBusy", "fabric", "1.21.4",
                                         accept_eula=True)
        modrinth.create_job("x.zip", 100, kind="pack", instance_id=inst["id"])
        dest = instances.pack_dir(inst["id"]) / "pack.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_cf_zip())
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.install_upload(inst["id"], dest, dest.name))
        assert e.value.status_code == 409
        assert "läuft bereits" in e.value.detail

    def test_upload_unbekannte_instanz_404(self, tmp_path):
        dest = tmp_path / "pack.zip"
        dest.write_bytes(_cf_zip())
        with pytest.raises(HTTPException) as e:
            asyncio.run(packs.install_upload("gibtsnicht", dest, dest.name))
        assert e.value.status_code == 404

    def test_upload_falsches_format(self, monkeypatch):
        """ZIP ohne bekannten Index → Job endet mit Fehler."""
        inst = instances.create_instance("UplBad", "fabric", "1.21.4",
                                         accept_eula=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("README.txt", "nix")
        dest = instances.pack_dir(inst["id"]) / "bad.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(buf.getvalue())

        job = _await_job(packs.install_upload(inst["id"], dest, dest.name))
        assert job["status"] == "error"
        assert "Modpack-Format" in job["error"]
        assert not dest.exists()  # Archiv wurde aufgeräumt

    def test_upload_route_multipart(self, client, monkeypatch):
        """POST /api/instances/{id}/modpacks/upload mit echtem Multipart-Body."""
        inst = instances.create_instance("UplApi", "fabric", "1.21.4",
                                         accept_eula=True)
        _patch_http(monkeypatch, _pack_handler({
            "/testmod.jar": (200, JAR),
            "/app.cfg": (200, CFG),
        }))
        resp = client.post(
            f"/api/instances/{inst['id']}/modpacks/upload",
            files={"file": ("Mein Pack.mrpack", _mrpack(_index()),
                            "application/octet-stream")},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["filename"] == "Mein Pack.mrpack"
        assert data["target"] == {"type": "instance", "instance_id": inst["id"]}
        for _ in range(100):
            job = client.get(f"/api/jobs/{data['job_id']}").json()
            if job["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert job["status"] == "done", job.get("error")
        root = instances.instance_dir(inst["id"])
        assert (root / "mods" / "testmod.jar").is_file()

    def test_upload_route_unbekannte_instanz_404(self, client):
        resp = client.post(
            "/api/instances/gibtsnicht/modpacks/upload",
            files={"file": ("pack.mrpack", _mrpack(_index()))},
        )
        assert resp.status_code == 404

    def test_upload_route_ungueltiger_dateiname_400(self, client):
        inst = instances.create_instance("UplName", "fabric", "1.21.4",
                                         accept_eula=True)
        resp = client.post(
            f"/api/instances/{inst['id']}/modpacks/upload",
            files={"file": ("../boese.zip", _cf_zip())},
        )
        assert resp.status_code == 400


class TestUploadAutoVersion:
    """Auto-Erkennung: Upload stellt die Instanz auf das Modpack um."""

    def test_passt_mc_version_automatisch_an(self, monkeypatch):
        inst = instances.create_instance("UplAuto", "fabric", "1.20.1",
                                         accept_eula=True)
        _patch_http(monkeypatch, _pack_handler({
            "/testmod.jar": (200, JAR),
            "/app.cfg": (200, CFG),
        }))
        dest = instances.pack_dir(inst["id"]) / "pack.mrpack"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_mrpack(_index(game_version="1.21.4")))

        job = _await_job(packs.install_upload(inst["id"], dest, dest.name))
        assert job["status"] == "done", job["error"]
        meta = instances.get_instance(inst["id"])
        assert meta["game_version"] == "1.21.4"
        assert meta["loader"] == "fabric"
        assert meta["loader_version"] == "0.16.9"
        assert meta["modpack"]["version_adapted"] == {
            "from": {"loader": "fabric", "game_version": "1.20.1"},
            "to": {"loader": "fabric", "game_version": "1.21.4"},
        }
        assert job["summary"]["version_adapted"]["to"]["game_version"] == "1.21.4"

    def test_passt_loader_automatisch_an(self, monkeypatch):
        inst = instances.create_instance("UplLoader", "paper", "1.21.4",
                                         accept_eula=True)
        _patch_http(monkeypatch, _pack_handler({
            "/testmod.jar": (200, JAR),
            "/app.cfg": (200, CFG),
        }))
        dest = instances.pack_dir(inst["id"]) / "pack.mrpack"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_mrpack(_index()))  # fabric-Loader im Pack

        job = _await_job(packs.install_upload(inst["id"], dest, dest.name))
        assert job["status"] == "done", job["error"]
        meta = instances.get_instance(inst["id"])
        assert meta["loader"] == "fabric"
        assert meta["game_version"] == "1.21.4"
        assert meta["modpack"]["version_adapted"]["from"]["loader"] == "paper"

    def test_laende_instanz_blockt_umstellung(self, monkeypatch):
        inst = instances.create_instance("UplLauf", "fabric", "1.20.1",
                                         accept_eula=True)
        monkeypatch.setattr("app.runtime.is_running", lambda _inst: True)
        dest = instances.pack_dir(inst["id"]) / "pack.mrpack"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_mrpack(_index(game_version="1.21.4")))

        job = _await_job(packs.install_upload(inst["id"], dest, dest.name))
        assert job["status"] == "error"
        assert "stoppen" in job["error"]
        meta = instances.get_instance(inst["id"])
        assert meta["game_version"] == "1.20.1"  # unverändert
        assert meta["modpack"] is None
        assert not dest.exists()  # Archiv wurde aufgeräumt

    def test_ohne_auto_version_inkompatibel(self, monkeypatch):
        inst = instances.create_instance("UplStrikt", "fabric", "1.21.4",
                                         accept_eula=True)
        _patch_http(monkeypatch, _pack_handler({
            "/testmod.jar": (200, JAR),
        }))
        dest = instances.pack_dir(inst["id"]) / "pack.mrpack"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_mrpack(_index(game_version="1.20.1")))

        job = _await_job(packs.install_upload(inst["id"], dest, dest.name,
                                              auto_version=False))
        assert job["status"] == "error"
        assert "Inkompatibel" in job["error"]
        assert instances.get_instance(inst["id"])["game_version"] == "1.21.4"

    def test_unterstuetzter_loader_pruefung(self):
        inst = instances.create_instance("UplLoaderX", "fabric", "1.20.1",
                                         accept_eula=True)
        with pytest.raises(HTTPException) as e:
            packs._adapt_instance_for_pack(
                inst, {"loader": "rift", "game_version": "1.21.4"})
        assert e.value.status_code == 400
        assert "rift" in e.value.detail

    def test_nichts_zu_tun_gibt_none(self):
        inst = instances.create_instance("UplGleich", "fabric", "1.21.4",
                                         accept_eula=True)
        assert packs._needs_version_adaptation(
            inst, {"loader": "fabric", "game_version": "1.21.4"}) is False
        assert packs._needs_version_adaptation(
            inst, {"loader": None, "game_version": "1.20.1"}) is False

    def test_upload_route_auto_version(self, client, monkeypatch):
        """Route: Standard = Anpassung an; auto_version=false = streng."""
        _patch_http(monkeypatch, _pack_handler({
            "/testmod.jar": (200, JAR),
            "/app.cfg": (200, CFG),
        }))
        inst = instances.create_instance("UplRouteA", "fabric", "1.21.4",
                                         accept_eula=True)
        resp = client.post(
            f"/api/instances/{inst['id']}/modpacks/upload",
            files={"file": ("pack.mrpack", _mrpack(_index(game_version="1.20.1")))},
        )
        assert resp.status_code == 200, resp.text
        job_id = resp.json()["job_id"]
        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert job["status"] == "done", job.get("error")
        assert instances.get_instance(inst["id"])["game_version"] == "1.20.1"

        inst2 = instances.create_instance("UplRouteB", "fabric", "1.21.4",
                                          accept_eula=True)
        resp = client.post(
            f"/api/instances/{inst2['id']}/modpacks/upload",
            files={"file": ("pack.mrpack", _mrpack(_index(game_version="1.20.1")))},
            data={"auto_version": "false"},
        )
        assert resp.status_code == 200, resp.text
        job_id = resp.json()["job_id"]
        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert job["status"] == "error"
        assert "Inkompatibel" in job["error"]
        assert instances.get_instance(inst2["id"])["game_version"] == "1.21.4"


class TestCurseForgeDownload:
    def test_redirect_bestimmt_dateinamen(self, tmp_path):
        client = httpx.AsyncClient(transport=httpx.MockTransport(_cf_routes()),
                                   follow_redirects=True)
        mods_root = tmp_path / "mods"
        name = asyncio.run(packs._download_cf_one(
            client,
            "https://www.curseforge.com/api/v1/mods/111/files/222/download",
            mods_root))
        assert name == "testmod.jar"
        assert (mods_root / "testmod.jar").read_bytes() == JAR

    def test_nicht_jar_wird_uebersprungen(self, tmp_path):
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/download"):
                return httpx.Response(302, headers={
                    "Location": "https://cdn.example/resourcepack.zip"})
            # finale CDN-URL: Ressourcenpaket statt Mod
            return httpx.Response(200, content=b"textur",
                                  headers={"Content-Type": "application/zip"})

        # finale URL ohne .jar → None
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                   follow_redirects=True)
        name = asyncio.run(packs._download_cf_one(
            client, "https://www.curseforge.com/api/v1/mods/1/files/2/download",
            tmp_path))
        assert name is None

    def test_http_fehler(self, tmp_path):
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(403)))
        with pytest.raises(RuntimeError, match="403"):
            asyncio.run(packs._download_cf_one(client, "https://cdn/x.jar",
                                               tmp_path))


class TestExtractOverrides:
    def test_erlaubte_und_verbotene_pfade(self, tmp_path):
        pack = tmp_path / "pack.zip"
        pack.write_bytes(_cf_zip(overrides={
            "config/app.cfg": CFG,
            "kubejs/test.js": b"// js",
            "../evil.jar": b"e",
            "saves/world.dat": b"w",
        }))
        extracted, skipped = packs._extract_overrides(pack, tmp_path)
        assert extracted == 2
        assert (tmp_path / "config" / "app.cfg").read_bytes() == CFG
        assert (tmp_path / "kubejs" / "test.js").read_bytes() == b"// js"
        assert len(skipped) == 2

    def test_leeres_archiv(self, tmp_path):
        pack = tmp_path / "pack.zip"
        pack.write_bytes(_cf_zip())  # nur manifest, keine overrides
        extracted, skipped = packs._extract_overrides(pack, tmp_path)
        assert extracted == 0
        assert skipped == []


class TestSafetySnapshot:
    """Sicherheits-Snapshot vor Modpack-Installationen (macht force risikofrei)."""

    def test_install_mit_mods_erstellt_pre_install_snapshot(self, monkeypatch):
        from app import backups as backups_mod
        inst = instances.create_instance("SnapSrv", "fabric", "1.21.4",
                                         accept_eula=True)
        mods = instances.mods_dir(inst["id"])
        mods.mkdir(parents=True, exist_ok=True)
        (mods / "alt.jar").write_text("alte-mod-version")
        pack = _mrpack(_index())
        _patch_http(monkeypatch, _pack_handler(_full_routes(pack)))

        job = _await_job(packs.install_pack(inst["id"], "packproj",
                                            version_id="v1"))
        assert job["status"] == "done", job["error"]
        names = [b["name"] for b in backups_mod.list_backups(inst["id"])]
        assert any(n.startswith("pre-install-") for n in names)
        # Die alte Mod-Version ist im Snapshot zurückholbar
        snapshot = next(n for n in names if n.startswith("pre-install-"))
        with tarfile.open(
                backups_mod.backup_path(inst["id"], snapshot)) as tar:
            content = tar.extractfile("./mods/alt.jar").read()
        assert content == b"alte-mod-version"

    def test_upload_install_mit_mods_erstellt_snapshot(self, monkeypatch):
        from app import backups as backups_mod
        inst = instances.create_instance("SnapUpl", "fabric", "1.21.4",
                                         accept_eula=True)
        mods = instances.mods_dir(inst["id"])
        mods.mkdir(parents=True, exist_ok=True)
        (mods / "vorhanden.jar").write_text("v")
        dest = instances.pack_dir(inst["id"]) / "Mein Pack.mrpack"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_mrpack(_index()))
        _patch_http(monkeypatch, _pack_handler({
            "/testmod.jar": (200, JAR),
            "/app.cfg": (200, CFG),
        }))
        job = _await_job(packs.install_upload(inst["id"], dest, dest.name))
        assert job["status"] == "done", job["error"]
        assert any(n.startswith("pre-install-") for n in
                   (b["name"] for b in backups_mod.list_backups(inst["id"])))

    def test_ohne_mods_kein_snapshot(self, monkeypatch):
        from app import backups as backups_mod
        inst = instances.create_instance("SnapLeer", "fabric", "1.21.4",
                                         accept_eula=True)
        pack = _mrpack(_index())
        _patch_http(monkeypatch, _pack_handler(_full_routes(pack)))
        job = _await_job(packs.install_pack(inst["id"], "packproj",
                                            version_id="v1"))
        assert job["status"] == "done", job["error"]
        assert backups_mod.list_backups(inst["id"]) == []

    def test_fehlgeschlagener_snapshot_bricht_install_ab(self, monkeypatch):
        from app import backups as backups_mod
        inst = instances.create_instance("SnapErr", "fabric", "1.21.4",
                                         accept_eula=True)
        mods = instances.mods_dir(inst["id"])
        mods.mkdir(parents=True, exist_ok=True)
        (mods / "alt.jar").write_text("alt")
        jobs_vorher = set(modrinth.JOBS)
        monkeypatch.setattr(backups_mod, "safety_backup",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("Platte voll")))
        pack = _mrpack(_index())
        _patch_http(monkeypatch, _pack_handler(_full_routes(pack)))
        with pytest.raises(RuntimeError, match="Platte voll"):
            asyncio.run(packs.install_pack(inst["id"], "packproj",
                                           version_id="v1"))
        # Kein Job gestartet, mods unangetastet
        assert set(modrinth.JOBS) == jobs_vorher
        assert (mods / "alt.jar").read_text() == "alt"
