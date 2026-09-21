"""Tests für die neuen Verwaltungs-Features: Klonen, Server-Import,
Welt-Download/Upload, JVM-Flags und Speicher-Aufschlüsselung."""
import io
import json
import shutil
import tarfile
import uuid
import zipfile

import pytest
from fastapi import HTTPException

from app import instances, runtime, worlds
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen():
    """Saubere Instanz-Umgebung pro Test."""
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    yield


def _create(name=None, **kwargs):
    kwargs.setdefault("loader", "fabric")
    kwargs.setdefault("game_version", "1.21.4")
    kwargs.setdefault("accept_eula", True)
    name = name or f"Feature-Test-{uuid.uuid4().hex[:8]}"
    return instances.create_instance(name, **kwargs)


def _seed_instance(d, world_name="world"):
    """Welt + Mod in die Instanz legen."""
    region = d / world_name / "region"
    region.mkdir(parents=True, exist_ok=True)
    (d / world_name / "level.dat").write_bytes(b"LEVELDAT")
    (region / "r.0.0.mca").write_bytes(b"x" * 100)
    (d / "mods").mkdir(exist_ok=True)
    (d / "mods" / "sodium.jar").write_bytes(b"y" * 50)


def _zip_bytes(entries: dict) -> bytes:
    """In-Memory-Zip: {arcname: bytes}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Klonen
# ---------------------------------------------------------------------------

class TestClone:
    def test_kopiert_welt_mods_configs_neue_id(self):
        src = _create(name="Klon-Quelle")
        d = instances.instance_dir(src["id"])
        _seed_instance(d)
        (d / "custom-config.txt").write_text("cfg")
        (d / "packs").mkdir(exist_ok=True)
        (d / "packs" / "pack.mrpack").write_bytes(b"z" * 10)

        clone = instances.clone_instance(src["id"])
        assert clone["id"] != src["id"]
        assert clone["name"] == "Klon-Quelle 2"
        assert clone["port"] != src["port"]
        assert clone["loader"] == src["loader"]
        assert clone["game_version"] == src["game_version"]
        assert clone["status"] == "stopped"
        cd = instances.instance_dir(clone["id"])
        assert (cd / "world" / "level.dat").read_bytes() == b"LEVELDAT"
        assert (cd / "mods" / "sodium.jar").is_file()
        assert (cd / "custom-config.txt").read_text() == "cfg"
        assert (cd / "eula.txt").read_text() == "eula=true\n"
        # packs/ wird nicht kopiert, aber als leerer Ordner angelegt
        assert (cd / "packs").is_dir()
        assert not (cd / "packs" / "pack.mrpack").exists()
        # Metadaten frisch
        meta = json.loads((cd / "instance.json").read_text())
        assert meta["id"] == clone["id"]

    def test_expliziter_name_kollision_409(self):
        src = _create(name="Koll")
        _create(name="Anderer")
        with pytest.raises(HTTPException) as e:
            instances.clone_instance(src["id"], name="anderer")
        assert e.value.status_code == 409

    def test_laufende_instanz_409(self, fake_docker):
        src = _create(name="Laeuft-Klon")
        fake_docker.containers.run(name=runtime.container_name(src["id"]),
                                   image="itzg/minecraft-server:latest")
        with pytest.raises(HTTPException) as e:
            instances.clone_instance(src["id"])
        assert e.value.status_code == 409

    def test_unbekannte_instanz_404(self):
        with pytest.raises(HTTPException) as e:
            instances.clone_instance("gibtsnicht")
        assert e.value.status_code == 404

    def test_api_clone(self, client):
        src = _create(name="API-Klon")
        _seed_instance(instances.instance_dir(src["id"]))
        r = client.post(f"/api/instances/{src['id']}/clone", json={"name": "Mein Klon"})
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "Mein Klon"
        assert body["id"] != src["id"]
        # Quelle bleibt unberührt
        assert client.get(f"/api/instances/{src['id']}").status_code == 200


# ---------------------------------------------------------------------------
# Einstellungen (PATCH) + JVM-Flags
# ---------------------------------------------------------------------------

class TestSettingsUndJvm:
    def test_update_jvm_und_aikar(self):
        inst = _create()
        result = instances.update_settings(
            inst["id"], jvm_opts=" -XX:+UseG1GC ", use_aikar=True)
        assert result["changed"] == ["jvm_opts", "use_aikar"]
        meta = instances.get_instance(inst["id"])
        assert meta["jvm_opts"] == "-XX:+UseG1GC"
        assert meta["use_aikar"] is True

    def test_jvm_leer_setzt_zurueck(self):
        inst = _create()
        instances.update_settings(inst["id"], jvm_opts="-Xmx1G")
        instances.update_settings(inst["id"], jvm_opts="")
        assert instances.get_instance(inst["id"])["jvm_opts"] is None

    def test_jvm_zeilenumbruch_abgelehnt(self):
        inst = _create()
        with pytest.raises(HTTPException) as e:
            instances.update_settings(inst["id"], jvm_opts="-Xmx1G\nEVIL=1")
        assert e.value.status_code == 400

    def test_jvm_zu_lang_abgelehnt(self):
        inst = _create()
        with pytest.raises(HTTPException) as e:
            instances.update_settings(inst["id"], jvm_opts="-X" * 1001)
        assert e.value.status_code == 400

    def test_umbenennen_und_kollision(self):
        inst = _create(name="Alt")
        _create(name="Belegt")
        instances.update_settings(inst["id"], name=" Neu ")
        assert instances.get_instance(inst["id"])["name"] == "Neu"
        with pytest.raises(HTTPException) as e:
            instances.update_settings(inst["id"], name="belegt")
        assert e.value.status_code == 409

    def test_env_enthaelt_jvm_opts(self):
        inst = dict(_create(), jvm_opts="-XX:+UseZGC", use_aikar=True)
        env = runtime._env(inst)
        assert "JVM_OPTS=-XX:+UseZGC" in env
        assert "USE_AIKAR_FLAGS=TRUE" in env

    def test_env_ohne_jvm_opts(self):
        inst = _create()
        env = runtime._env(inst)
        assert not any(e.startswith("JVM_OPTS=") for e in env)
        assert not any(e.startswith("USE_AIKAR_FLAGS=") for e in env)

    def test_api_patch(self, client):
        inst = _create(name="Patch-Ich")
        r = client.patch(f"/api/instances/{inst['id']}", json={
            "jvm_opts": "-XX:+UseG1GC", "use_aikar": True})
        assert r.status_code == 200
        assert r.json()["jvm_opts"] == "-XX:+UseG1GC"
        # Namen separat patchen
        r = client.patch(f"/api/instances/{inst['id']}", json={"name": "Patch-II"})
        assert r.status_code == 200
        assert r.json()["name"] == "Patch-II"
        # Leerer Body → 400
        assert client.patch(f"/api/instances/{inst['id']}", json={}).status_code == 400
        # Ungültige Flags → 400
        assert client.patch(f"/api/instances/{inst['id']}",
                            json={"jvm_opts": "a\nb"}).status_code == 400

    def test_patch_unbekannt_404(self, client):
        assert client.patch("/api/instances/gibtsnicht",
                            json={"use_aikar": True}).status_code == 404


# ---------------------------------------------------------------------------
# Welt: Erkennung, Speicher, Download/Upload
# ---------------------------------------------------------------------------

class TestWelt:
    def test_find_world_dir_über_level_name(self):
        d = instances.instance_dir(_create()["id"])
        (d / "server.properties").write_text("level-name=survival\n", encoding="utf-8")
        (d / "survival").mkdir()
        (d / "survival" / "level.dat").write_bytes(b"x")
        assert instances.find_world_dir(d).name == "survival"

    def test_find_world_dir_fallback(self):
        d = instances.instance_dir(_create()["id"])
        (d / "meine-welt").mkdir()
        (d / "meine-welt" / "level.dat").write_bytes(b"x")
        assert instances.find_world_dir(d).name == "meine-welt"

    def test_find_world_dir_keine_welt(self):
        d = instances.instance_dir(_create()["id"])
        assert instances.find_world_dir(d) is None

    def test_disk_usage_aufschlüsselung(self):
        inst = _create()
        d = instances.instance_dir(inst["id"])
        _seed_instance(d)
        (d / "packs").mkdir(exist_ok=True)
        (d / "packs" / "pack.mrpack").write_bytes(b"p" * 30)
        (d / "server.properties").write_text("motd=x\n", encoding="utf-8")
        usage = instances.disk_usage(inst["id"])
        assert usage["world_exists"] and usage["world_dir"] == "world"
        assert usage["mods_bytes"] == 50
        assert usage["world_bytes"] == 108  # level.dat + mca
        assert usage["packs_bytes"] == 30
        assert usage["total_bytes"] >= usage["mods_bytes"] + usage["world_bytes"] \
            + usage["packs_bytes"]
        assert usage["rest_bytes"] >= 0

    def test_api_detail_enthaelt_disk(self, client):
        inst = _create(name="Disk-Ich")
        _seed_instance(instances.instance_dir(inst["id"]))
        r = client.get(f"/api/instances/{inst['id']}")
        assert r.status_code == 200
        disk = r.json()["disk"]
        assert disk["world_exists"] is True
        assert disk["mods_bytes"] == 50

    def test_download_ohne_welt_404(self, client):
        inst = _create(name="Ohne-Welt")
        r = client.get(f"/api/instances/{inst['id']}/world")
        assert r.json()["exists"] is False
        r = client.get(f"/api/instances/{inst['id']}/world/download")
        assert r.status_code == 404

    def test_download_zip(self, client):
        inst = _create(name="Welt-DL")
        _seed_instance(instances.instance_dir(inst["id"]))
        r = client.get(f"/api/instances/{inst['id']}/world/download")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/zip")
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = zf.namelist()
        assert "level.dat" in names
        assert any(n.startswith("region/") for n in names)

    def test_upload_unterordner_layout(self, client):
        inst = _create(name="Welt-UL")
        d = instances.instance_dir(inst["id"])
        _seed_instance(d)  # alte Welt "world"
        data = _zip_bytes({
            "survival/level.dat": b"NEU",
            "survival/region/r.0.0.mca": b"neu-mca",
            "ignored.txt": b"nicht Teil der Welt",
        })
        r = client.post(f"/api/instances/{inst['id']}/world/upload",
                        files={"file": ("welt.zip", data, "application/zip")})
        assert r.status_code == 200
        assert r.json()["world_dir"] == "survival"
        assert (d / "survival" / "level.dat").read_bytes() == b"NEU"
        assert (d / "world").exists() is False  # alte Welt entfernt? level-name zeigt auf survival
        props = (d / "server.properties").read_text()
        assert "level-name=survival" in props

    def test_upload_root_layout(self, client):
        inst = _create(name="Welt-UL-Root")
        d = instances.instance_dir(inst["id"])
        data = _zip_bytes({
            "level.dat": b"ROOT-WELT",
            "region/r.0.0.mca": b"mca",
        })
        r = client.post(f"/api/instances/{inst['id']}/world/upload",
                        files={"file": ("welt.zip", data, "application/zip")})
        assert r.status_code == 200
        assert r.json()["world_dir"] == "world"
        assert (d / "world" / "level.dat").read_bytes() == b"ROOT-WELT"
        assert "level-name=world" in (d / "server.properties").read_text()

    def test_upload_tar_gz(self, client):
        inst = _create(name="Welt-UL-Tar")
        d = instances.instance_dir(inst["id"])
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            content = b"TAR-WELT"
            info = tarfile.TarInfo("welt/level.dat")
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        r = client.post(f"/api/instances/{inst['id']}/world/upload",
                        files={"file": ("welt.tar.gz", buf.getvalue(),
                                        "application/gzip")})
        assert r.status_code == 200
        assert r.json()["world_dir"] == "welt"
        assert (d / "welt" / "level.dat").read_bytes() == b"TAR-WELT"

    def test_upload_laufende_instanz_409(self, client, fake_docker):
        inst = _create(name="Welt-UL-Laeuft")
        fake_docker.containers.run(name=runtime.container_name(inst["id"]),
                                   image="itzg/minecraft-server:latest")
        data = _zip_bytes({"level.dat": b"x"})
        r = client.post(f"/api/instances/{inst['id']}/world/upload",
                        files={"file": ("welt.zip", data, "application/zip")})
        assert r.status_code == 409

    def test_upload_ohne_level_dat_400(self, client):
        inst = _create(name="Welt-UL-Leer")
        data = _zip_bytes({"irgendwas.txt": b"x"})
        r = client.post(f"/api/instances/{inst['id']}/world/upload",
                        files={"file": ("welt.zip", data, "application/zip")})
        assert r.status_code == 400

    def test_upload_path_traversal_abgelehnt(self, client):
        inst = _create(name="Welt-UL-Evil")
        data = _zip_bytes({"../evil.txt": b"boese"})
        r = client.post(f"/api/instances/{inst['id']}/world/upload",
                        files={"file": ("evil.zip", data, "application/zip")})
        assert r.status_code == 400
        # nichts außerhalb des Instanz-Ordners geschrieben
        root = settings.instances_dir.resolve()
        for entry in root.iterdir():
            assert entry.name != "evil.txt"

    def test_upload_ungueltiger_dateiname(self, client):
        inst = _create(name="Welt-UL-Name")
        r = client.post(f"/api/instances/{inst['id']}/world/upload",
                        files={"file": ("welt.exe", b"x", "application/zip")})
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Server-Import
# ---------------------------------------------------------------------------

class TestImport:
    def test_import_mit_welt_unterordner_und_mods(self, client):
        data = _zip_bytes({
            "meine-welt/level.dat": b"WELT",
            "meine-welt/region/r.0.0.mca": b"mca",
            "mods/sodium.jar": b"mod",
            "server.properties": "level-name=meine-welt\nserver-port=25000\n",
        })
        r = client.post("/api/instances/import", data={
            "name": "Import-1", "loader": "fabric", "game_version": "1.21.4",
            "accept_eula": "true",
        }, files={"file": ("server.zip", data, "application/zip")})
        assert r.status_code == 201
        inst = r.json()
        d = instances.instance_dir(inst["id"])
        assert (d / "meine-welt" / "level.dat").read_bytes() == b"WELT"
        assert (d / "mods" / "sodium.jar").is_file()
        props = (d / "server.properties").read_text()
        assert "level-name=meine-welt" in props
        assert "server-port=25565" in props
        assert "enable-rcon=true" in props
        assert "rcon.port=25575" in props
        assert (d / "eula.txt").read_text() == "eula=true\n"

    def test_import_welt_am_wurzelverzeichnis_wird_umgezogen(self, client):
        data = _zip_bytes({
            "level.dat": b"WURZEL",
            "region/r.0.0.mca": b"mca",
            "playerdata/steve.dat": b"player",
        })
        r = client.post("/api/instances/import", data={
            "name": "Import-Root", "loader": "paper", "game_version": "1.21.4",
            "accept_eula": "true",
        }, files={"file": ("server.zip", data, "application/zip")})
        assert r.status_code == 201
        d = instances.instance_dir(r.json()["id"])
        assert (d / "world" / "level.dat").read_bytes() == b"WURZEL"
        assert (d / "world" / "region" / "r.0.0.mca").read_bytes() == b"mca"
        assert (d / "world" / "playerdata" / "steve.dat").read_bytes() == b"player"
        props = (d / "server.properties").read_text()
        assert "level-name=world" in props

    def test_import_name_kollision_409(self, client):
        _create(name="Schon-Da")
        data = _zip_bytes({"level.dat": b"x"})
        r = client.post("/api/instances/import", data={
            "name": "schon-da", "loader": "fabric", "game_version": "1.21.4",
            "accept_eula": "true",
        }, files={"file": ("server.zip", data, "application/zip")})
        assert r.status_code == 409

    def test_import_eula_pflicht(self, client):
        data = _zip_bytes({"level.dat": b"x"})
        r = client.post("/api/instances/import", data={
            "name": "Import-Ohne-Eula", "loader": "fabric",
            "game_version": "1.21.4", "accept_eula": "false",
        }, files={"file": ("server.zip", data, "application/zip")})
        assert r.status_code == 400

    def test_import_traversal_bereinigt_instanz(self, client):
        data = _zip_bytes({"../evil.jar": b"boese"})
        r = client.post("/api/instances/import", data={
            "name": "Import-Evil", "loader": "fabric", "game_version": "1.21.4",
            "accept_eula": "true",
        }, files={"file": ("evil.zip", data, "application/zip")})
        assert r.status_code == 400
        # fehlgeschlagene Instanz vollständig entfernt
        names = [i["name"] for i in instances.list_instances()]
        assert "Import-Evil" not in names
        root = settings.instances_dir.resolve()
        for entry in root.iterdir():
            assert entry.name != "evil.jar"

    def test_import_ungueltiger_loader(self, client):
        data = _zip_bytes({"level.dat": b"x"})
        r = client.post("/api/instances/import", data={
            "name": "Import-BadLoader", "loader": "optifine",
            "game_version": "1.21.4", "accept_eula": "true",
        }, files={"file": ("server.zip", data, "application/zip")})
        assert r.status_code == 400

    def test_import_ohne_welt_funktioniert(self, client):
        data = _zip_bytes({"mods/sodium.jar": b"mod",
                           "config/option.cfg": b"cfg"})
        r = client.post("/api/instances/import", data={
            "name": "Import-Ohne-Welt", "loader": "fabric",
            "game_version": "1.21.4", "accept_eula": "true",
        }, files={"file": ("server.zip", data, "application/zip")})
        assert r.status_code == 201
        d = instances.instance_dir(r.json()["id"])
        assert (d / "mods" / "sodium.jar").is_file()
        assert (d / "config" / "option.cfg").read_bytes() == b"cfg"


# ---------------------------------------------------------------------------
# worlds: Validierung der Archiv-Namen
# ---------------------------------------------------------------------------

class TestArchiveValidation:
    def test_gueltige_namen(self):
        assert worlds.validate_archive_filename("server.zip") == "server.zip"
        assert worlds.validate_archive_filename("Mein Server (2024).tar.gz") \
            == "Mein Server (2024).tar.gz"

    def test_ungueltige_namen(self):
        for name in ("", "a.exe", "a.zip.exe", "../a.zip", "a/b.zip",
                     "a" * 300 + ".zip", "a.zip\x00"):
            with pytest.raises(HTTPException):
                worlds.validate_archive_filename(name)
