"""Tests für den Modpack-Upload: Server-Files-Erkennung, Vorschau (analyze),
RAM-Empfehlung, Teilfehler und „fehlende erneut laden“."""
import asyncio
import io
import json
import shutil
import zipfile

import httpx
import pytest

from app import instances, modrinth, packs
from app.config import settings

JAR = b"fake-jar-inhalt" * 10


@pytest.fixture(autouse=True)
def _leere_umgebung():
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


def _await_job(coro):
    async def _flow():
        job = await coro
        for _ in range(500):
            if job["status"] in ("done", "error"):
                return job
            await asyncio.sleep(0.02)
        return job

    return asyncio.run(_flow())


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def _mod_jar(toml: str = None, fabric: dict = None) -> bytes:
    files = {}
    if toml is not None:
        files["META-INF/neoforge.mods.toml"] = toml
    if fabric is not None:
        files["fabric.mod.json"] = json.dumps(fabric)
    return _zip(files)


_NEO_TOML = """
modLoader = "javafml"
loaderVersion = "[1,)"
[[mods]]
modId = "beispiel"
version = "1.0"
[[dependencies.beispiel]]
modId = "minecraft"
type = "required"
versionRange = "[1.21.1,1.22)"
"""


def _atm10_server_files(prefix: str = "Server-Files-4.11/") -> bytes:
    """Nachbau der ATM10-Server-Files: variables.txt, Startskripte, mods/."""
    return _zip({
        f"{prefix}variables.txt": (
            "### All the Mods 10\n"
            "MINECRAFT_VERSION=1.21.1\n"
            "MODLOADER=NeoForge\n"
            'MODLOADER_VERSION="21.1.209"\n'),
        f"{prefix}startserver.sh": "#!/bin/sh\n",
        f"{prefix}startserver.bat": "@echo off\n",
        f"{prefix}server-icon.png": b"png",
        f"{prefix}mods/allthemodium.jar": JAR,
        f"{prefix}mods/mekanism.jar": JAR,
        f"{prefix}config/allthemodium.toml": "a = 1\n",
        f"{prefix}defaultconfigs/ftb.snbt": "{}\n",
        f"{prefix}kubejs/server_scripts/x.js": "// x\n",
        f"{prefix}libraries/foo.jar": b"lib",
    })


def _detect(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return packs._detect_server_pack(archive)


class TestServerPackErkennung:
    def test_atm10_variables_txt_im_unterordner(self):
        info = _detect(_atm10_server_files())
        assert info["prefix"] == "Server-Files-4.11/"
        assert info["loader"] == "neoforge"
        assert info["game_version"] == "1.21.1"
        assert info["loader_version"] == "21.1.209"
        assert info["mod_count"] == 2
        assert info["detected_by"] == "variables.txt"

    def test_ohne_unterordner(self):
        info = _detect(_atm10_server_files(prefix=""))
        assert info["prefix"] == ""
        assert info["loader"] == "neoforge"

    def test_neoforge_installer_name(self):
        info = _detect(_zip({
            "neoforge-21.1.172-installer.jar": b"x",
            "mods/a.jar": JAR,
        }))
        assert (info["loader"], info["game_version"], info["loader_version"]) == \
            ("neoforge", "1.21.1", "21.1.172")

    def test_neoforge_x_0_ergibt_kurze_mc_version(self):
        assert packs._neoforge_mc("21.0.167") == "1.21"
        assert packs._neoforge_mc("20.4.237") == "1.20.4"
        assert packs._neoforge_mc("47.1.106") is None
        assert packs._neoforge_mc("26.1.0.12-beta") == "26.1"
        assert packs._neoforge_mc("26.1.1.3") == "26.1.1"

    def test_forge_installer_name(self):
        info = _detect(_zip({
            "forge-1.20.1-47.3.0-installer.jar": b"x",
            "mods/a.jar": JAR,
        }))
        assert (info["loader"], info["game_version"], info["loader_version"]) == \
            ("forge", "1.20.1", "47.3.0")

    def test_fabric_launcher_name(self):
        info = _detect(_zip({
            "fabric-server-mc.1.21.4-loader.0.16.9-launcher.1.0.1.jar": b"x",
            "mods/a.jar": JAR,
        }))
        assert (info["loader"], info["game_version"]) == ("fabric", "1.21.4")

    def test_libraries_neoforge(self):
        info = _detect(_zip({
            "libraries/net/neoforged/neoforge/21.1.200/unix_args.txt": "x",
            "mods/a.jar": JAR,
        }))
        assert (info["loader"], info["game_version"]) == ("neoforge", "1.21.1")

    def test_fallback_mod_metadaten(self):
        info = _detect(_zip({
            "mods/a.jar": _mod_jar(toml=_NEO_TOML),
            "mods/b.jar": _mod_jar(toml=_NEO_TOML),
        }))
        assert (info["loader"], info["game_version"]) == ("neoforge", "1.21.1")
        assert info["detected_by"] == "Mod-Metadaten"

    def test_fallback_fabric(self):
        info = _detect(_zip({
            "mods/a.jar": _mod_jar(fabric={"id": "a", "depends": {"minecraft": "~1.21.4"}}),
        }))
        assert (info["loader"], info["game_version"]) == ("fabric", "1.21.4")

    def test_kein_mods_ordner(self):
        assert _detect(_zip({"README.txt": "x"})) is None

    def test_zwei_unterordner_mehrdeutig(self):
        assert _detect(_zip({"a/mods/x.jar": JAR, "b/mods/y.jar": JAR})) is None

    def test_archiv_im_archiv_klare_meldung(self, tmp_path):
        path = tmp_path / "doppelt.zip"
        path.write_bytes(_zip({"ServerFiles.zip": b"PK"}))
        with pytest.raises(Exception) as err:
            packs._extract_index(path)
        assert "weiteres Archiv" in str(err.value.detail)


class TestServerPackInstallation:
    def test_server_files_installieren_und_umstellen(self):
        inst = instances.create_instance("ATM", "fabric", "1.21.4", accept_eula=True)
        dest = instances.pack_dir(inst["id"]) / "ServerFiles-4.11.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_atm10_server_files())

        job = _await_job(packs.install_upload(inst["id"], dest, dest.name))
        assert job["status"] == "done", job["error"]
        root = instances.instance_dir(inst["id"])
        assert (root / "mods" / "allthemodium.jar").read_bytes() == JAR
        assert (root / "config" / "allthemodium.toml").is_file()
        assert (root / "defaultconfigs" / "ftb.snbt").is_file()
        assert (root / "kubejs" / "server_scripts" / "x.js").is_file()
        assert not (root / "startserver.sh").exists()
        assert not (root / "libraries").exists()
        meta = instances.get_instance(inst["id"])
        assert meta["loader"] == "neoforge"
        assert meta["game_version"] == "1.21.1"
        assert meta["loader_version"] == "21.1.209"
        assert meta["modpack"]["format"] == "serverpack"
        assert meta["modpack"]["files"] == 2
        assert any("startserver.sh" in s for s in job["summary"]["skipped"])

    def test_neuer_server_aus_server_files_mit_ram_empfehlung(self, monkeypatch):
        monkeypatch.setattr(packs, "recommend_memory", lambda n: "12G")
        archive = packs.staging_dir() / "upload_test_ServerFiles.zip"
        archive.write_bytes(_atm10_server_files())

        async def flow():
            result = await packs.create_server_from_upload(
                archive, "ServerFiles.zip", accept_eula=True)
            job = result["job"]
            for _ in range(500):
                if job["status"] in ("done", "error"):
                    break
                await asyncio.sleep(0.02)
            return result

        result = asyncio.run(flow())
        inst = result["instance"]
        assert result["job"]["status"] == "done", result["job"]["error"]
        assert inst["name"] == "Server-Files-4.11"
        assert (inst["loader"], inst["game_version"]) == ("neoforge", "1.21.1")
        assert inst["memory"] == "12G"


class TestRamEmpfehlung:
    @pytest.mark.parametrize(("mods", "expected"), [
        (0, None), (10, "4G"), (59, "4G"), (60, "6G"), (149, "6G"),
        (150, "8G"), (249, "8G"), (250, "12G"), (450, "12G"),
    ])
    def test_stufen(self, mods, expected):
        assert packs.recommend_memory(mods) == expected


_CF_TWO = {
    "minecraft": {"version": "1.21.4",
                  "modLoaders": [{"id": "fabric-0.16.9", "primary": True}]},
    "manifestType": "minecraftModpack",
    "name": "Zwei Mods",
    "version": "1.0",
    "files": [{"projectID": 111, "fileID": 222, "required": True},
              {"projectID": 333, "fileID": 444, "required": True}],
}


def _cf_handler(broken: set):
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for pid, fid, name in ((111, 222, "gut.jar"), (333, 444, "kaputt.jar")):
            if path == f"/api/v1/mods/{pid}/files/{fid}/download":
                if name in broken:
                    return httpx.Response(404, content=b"weg")
                return httpx.Response(302, headers={
                    "Location": f"https://mediafilez.forgecdn.net/files/1/{name}"})
            if path.endswith(f"/{name}"):
                return httpx.Response(200, content=JAR)
        return httpx.Response(404, content=b"not found")

    return handle


class TestTeilfehler:
    def _upload(self, inst):
        dest = instances.pack_dir(inst["id"]) / "zwei.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_zip({"manifest.json": json.dumps(_CF_TWO)}))
        return _await_job(packs.install_upload(inst["id"], dest, dest.name))

    def test_einzelner_fehler_bricht_nicht_ab_und_retry_laedt_nach(self, monkeypatch):
        inst = instances.create_instance("Teil", "fabric", "1.21.4", accept_eula=True)
        broken = {"kaputt.jar"}
        _patch_http(monkeypatch, _cf_handler(broken))
        job = self._upload(inst)
        assert job["status"] == "done", job["error"]
        assert len(job["summary"]["failed"]) == 1
        root = instances.instance_dir(inst["id"])
        assert (root / "mods" / "gut.jar").is_file()
        meta = instances.get_instance(inst["id"])
        assert len(meta["modpack"]["failed"]) == 1

        # Datei wieder erreichbar → nur die fehlende wird nachgeladen
        broken.clear()
        retry = _await_job(packs.retry_failed(inst["id"]))
        assert retry["status"] == "done", retry["error"]
        assert retry["summary"]["files"] == 1
        assert (root / "mods" / "kaputt.jar").is_file()
        meta = instances.get_instance(inst["id"])
        assert "failed" not in meta["modpack"]
        assert meta["modpack"]["files"] == 2

    def test_alles_fehlgeschlagen_ist_fehler(self, monkeypatch):
        inst = instances.create_instance("Alle", "fabric", "1.21.4", accept_eula=True)
        _patch_http(monkeypatch, _cf_handler({"gut.jar", "kaputt.jar"}))
        job = self._upload(inst)
        assert job["status"] == "error"
        assert "2 Mod-Datei(en) fehlgeschlagen" in job["error"]

    def test_retry_ohne_fehlende_dateien(self):
        inst = instances.create_instance("Leer", "fabric", "1.21.4", accept_eula=True)
        with pytest.raises(Exception) as err:
            asyncio.run(packs.retry_failed(inst["id"]))
        assert err.value.status_code == 400

    def test_retry_ignoriert_fremde_urls(self):
        root = instances.instance_dir("x")
        assert packs._valid_failed_entry(root, {"kind": "cf", "url": "https://evil.example/a"}) is None
        assert packs._valid_failed_entry(root, {"kind": "mr", "url": "https://cdn.example/a",
                                                "path": "../../etc/passwd"}) is None
        assert packs._valid_failed_entry(root, {"kind": "mr", "url": "http://cdn.example/a",
                                                "path": "mods/a.jar"}) is None


class TestVorschauRoute:
    def test_analyze_und_server_aus_token(self, client):
        resp = client.post(
            "/api/modpacks/analyze",
            files={"file": ("ServerFiles-4.11.zip", _atm10_server_files(),
                            "application/zip")})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["format"] == "serverpack"
        assert data["loader"] == "neoforge"
        assert data["game_version"] == "1.21.1"
        assert data["java"] == "21"
        assert data["mods"] == 2
        assert data["recommended_memory"] == "4G"
        assert data["errors"] == []
        token = data["token"]
        assert list(packs.staging_dir().glob(f"preview_{token}_*"))

        resp = client.post("/api/instances/from-pack-upload",
                           data={"staged": token, "accept_eula": "true"})
        assert resp.status_code == 201, resp.text
        inst = resp.json()["instance"]
        assert inst["memory"] == "4G"
        assert (inst["loader"], inst["game_version"]) == ("neoforge", "1.21.1")
        assert not list(packs.staging_dir().glob(f"preview_{token}_*"))

    def test_analyze_client_export_warnt(self, client):
        resp = client.post(
            "/api/modpacks/analyze",
            files={"file": ("zwei.zip", _zip({"manifest.json": json.dumps(_CF_TWO)}),
                            "application/zip")})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["format"] == "curseforge"
        assert data["title"] == "Zwei Mods"
        assert data["version"] == "1.0"
        assert data["mods"] == 2
        assert any("Server Files" in w for w in data["warnings"])

    def test_analyze_unbekanntes_archiv(self, client):
        resp = client.post(
            "/api/modpacks/analyze",
            files={"file": ("x.zip", _zip({"README.txt": "x"}), "application/zip")})
        assert resp.status_code == 400
        assert "Kein Modpack erkannt" in resp.json()["detail"]
        assert not list(packs.staging_dir().glob("preview_*x.zip"))

    def test_token_ungueltig_oder_abgelaufen(self, client):
        resp = client.post("/api/instances/from-pack-upload",
                           data={"staged": "../../etc", "accept_eula": "true"})
        assert resp.status_code == 400
        resp = client.post("/api/instances/from-pack-upload",
                           data={"staged": "0123456789abcdef", "accept_eula": "true"})
        assert resp.status_code == 404

    def test_ohne_datei_und_token(self, client):
        resp = client.post("/api/instances/from-pack-upload",
                           data={"accept_eula": "true"})
        assert resp.status_code == 400

    def test_bestehende_instanz_mit_token(self, client):
        inst = instances.create_instance("Ziel", "neoforge", "1.21.1", accept_eula=True)
        token = client.post(
            "/api/modpacks/analyze",
            files={"file": ("ServerFiles.zip", _atm10_server_files(), "application/zip")},
        ).json()["token"]
        resp = client.post(f"/api/instances/{inst['id']}/modpacks/upload",
                           data={"staged": token})
        assert resp.status_code == 200, resp.text
        assert resp.json()["job_id"]
        assert not list(packs.staging_dir().glob(f"preview_{token}_*"))
