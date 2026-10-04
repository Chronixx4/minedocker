"""Tests für die Live-Weltkarte (BlueMap): Ein-/Ausschalten, Installation
(Modrinth gemockt), Port-Reservierung, Container-Port und Welt-Wechsel."""
import shutil

import pytest
from fastapi import HTTPException

from app import instances, livemap, modrinth, runtime
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen():
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    yield


@pytest.fixture()
def modrinth_fake(monkeypatch):
    """Modrinth-Antworten + Downloads ohne Netz."""
    calls = {"versions": [], "deps": [], "downloads": []}

    async def fake_get_json(client, path, params=None):
        calls["versions"].append((path, params))
        return [{"id": "VER1", "project_id": "swbUV1cr",
                 "files": [{"filename": "bluemap-5.7-fabric.jar", "primary": True,
                            "url": "https://cdn.modrinth.com/bluemap.jar", "size": 3,
                            "hashes": {"sha1": "abc"}}]}]

    async def fake_deps(project_id, version_id, loader, game_version, installed_project_ids=None):
        calls["deps"].append((project_id, version_id, loader))
        return {"files": [{"filename": "fabric-api-0.119.2+1.21.4.jar",
                           "url": "https://cdn.modrinth.com/fapi.jar", "size": 3,
                           "sha1": None, "project_id": "P7dR8mSH", "version_id": "F1"}],
                "skipped": []}

    async def fake_download(job, url, dest, sha1=None, verify_url=False, expected=0):
        calls["downloads"].append(dest.name)
        dest.write_bytes(b"jar")
        return True

    monkeypatch.setattr(modrinth, "_get_json", fake_get_json)
    monkeypatch.setattr(modrinth, "dependency_files", fake_deps)
    monkeypatch.setattr(modrinth, "_download_one", fake_download)
    return calls


def _create(loader="fabric", port=None):
    return instances.create_instance(f"Karte-{loader}-{port or 'auto'}", loader, "1.21.4",
                                     port=port, accept_eula=True)


class TestEinschalten:
    def test_ohne_zustimmung_abgelehnt(self, client, modrinth_fake):
        inst = _create()
        r = client.put(f"/api/instances/{inst['id']}/map", json={"enabled": True})
        assert r.status_code == 400
        assert modrinth_fake["downloads"] == []

    def test_fabric_installiert_mod_und_fabric_api(self, client, modrinth_fake):
        inst = _create()
        r = client.put(f"/api/instances/{inst['id']}/map",
                       json={"enabled": True, "accept_download": True})
        assert r.status_code == 200, r.text
        d = instances.instance_dir(inst["id"])
        assert (d / "mods" / "bluemap-5.7-fabric.jar").exists()
        assert (d / "mods" / "fabric-api-0.119.2+1.21.4.jar").exists()
        core = (d / "config" / "bluemap" / "core.conf").read_text()
        assert "accept-download: true" in core
        meta = instances.get_instance(inst["id"])["map"]
        assert meta["enabled"] is True and meta["port"] == inst["port"] + 2000
        body = r.json()
        assert body["enabled"] and body["installed"] and body["download_accepted"]
        assert body["kind"] == "mod"

    def test_vorhandene_fabric_api_wird_nicht_doppelt_geladen(self, client, modrinth_fake):
        inst = _create()
        mods = instances.instance_dir(inst["id"]) / "mods"
        mods.mkdir(exist_ok=True)
        (mods / "fabric-api-0.110.0+1.21.4.jar").write_bytes(b"alt")
        client.put(f"/api/instances/{inst['id']}/map",
                   json={"enabled": True, "accept_download": True})
        assert modrinth_fake["downloads"] == ["bluemap-5.7-fabric.jar"]

    def test_paper_als_plugin_ohne_abhaengigkeiten(self, client, modrinth_fake):
        inst = _create(loader="paper")
        r = client.put(f"/api/instances/{inst['id']}/map",
                       json={"enabled": True, "accept_download": True})
        assert r.status_code == 200, r.text
        d = instances.instance_dir(inst["id"])
        assert (d / "plugins" / "bluemap-5.7-fabric.jar").exists()
        assert (d / "plugins" / "BlueMap" / "core.conf").is_file()
        assert modrinth_fake["deps"] == []
        assert '"paper"' in modrinth_fake["versions"][0][1]["loaders"]

    def test_bestehende_core_conf_wird_umgestellt(self, client, modrinth_fake):
        inst = _create()
        conf = instances.instance_dir(inst["id"]) / "config" / "bluemap"
        conf.mkdir(parents=True)
        (conf / "core.conf").write_text("accept-download: false\nrender-thread-count: 4\n")
        client.put(f"/api/instances/{inst['id']}/map",
                   json={"enabled": True, "accept_download": True})
        text = (conf / "core.conf").read_text()
        assert "accept-download: true" in text and "render-thread-count: 4" in text


class TestPortsUndStart:
    def test_neue_instanz_nimmt_nicht_den_kartenport(self, client, modrinth_fake):
        inst = _create(port=25570)
        client.put(f"/api/instances/{inst['id']}/map",
                   json={"enabled": True, "accept_download": True})
        assert instances.get_instance(inst["id"])["map"]["port"] == 27570
        with pytest.raises(HTTPException) as exc:
            _create(port=27570)
        assert exc.value.status_code == 409

    def test_start_veroeffentlicht_kartenport(self, client, modrinth_fake, fake_docker):
        inst = _create()
        client.put(f"/api/instances/{inst['id']}/map",
                   json={"enabled": True, "accept_download": True})
        runtime.start_instance(instances.get_instance(inst["id"]))
        ports = fake_docker.containers.run_kwargs["ports"]
        assert ports["8100/tcp"] == inst["port"] + 2000

    def test_ausschalten_entfernt_jar_und_port(self, client, modrinth_fake, fake_docker):
        inst = _create()
        client.put(f"/api/instances/{inst['id']}/map",
                   json={"enabled": True, "accept_download": True})
        r = client.put(f"/api/instances/{inst['id']}/map", json={"enabled": False})
        assert r.status_code == 200 and r.json()["enabled"] is False
        d = instances.instance_dir(inst["id"])
        assert not (d / "mods" / "bluemap-5.7-fabric.jar").exists()
        assert (d / "mods" / "fabric-api-0.119.2+1.21.4.jar").exists()  # bleibt
        runtime.start_instance(instances.get_instance(inst["id"]))
        assert "8100/tcp" not in fake_docker.containers.run_kwargs["ports"]

    def test_weltwechsel_setzt_karte_zurueck(self, client, modrinth_fake, fake_docker):
        inst = _create()
        client.put(f"/api/instances/{inst['id']}/map",
                   json={"enabled": True, "accept_download": True})
        d = instances.instance_dir(inst["id"])
        maps_conf = d / "config" / "bluemap" / "maps"
        maps_data = d / "bluemap" / "web" / "maps"
        for path in (maps_conf, maps_data):
            path.mkdir(parents=True)
            (path / "x").write_text("alt")
        # gleicher Welt-Name → nichts zurücksetzen
        runtime.start_instance(instances.get_instance(inst["id"]))
        assert (maps_conf / "x").exists()
        fake_docker.containers.get(runtime.container_name(inst["id"])).stop()
        (d / "server.properties").write_text("level-name=neu\n")
        runtime.start_instance(instances.get_instance(inst["id"]))
        assert not maps_conf.exists() and not maps_data.exists()
        assert instances.get_instance(inst["id"])["map"]["world"] == "neu"


class TestStatus:
    def test_status_ausgeschaltet(self, client, fake_docker):
        inst = _create()
        r = client.get(f"/api/instances/{inst['id']}/map")
        assert r.status_code == 200
        body = r.json()
        assert body["supported"] and not body["enabled"] and not body["reachable"]
        assert body["server_running"] is False

    def test_erreichbar_nur_wenn_webserver_antwortet(self, client, modrinth_fake,
                                                      fake_docker, monkeypatch):
        inst = _create()
        client.put(f"/api/instances/{inst['id']}/map",
                   json={"enabled": True, "accept_download": True})
        runtime.start_instance(instances.get_instance(inst["id"]))

        async def up(instance):
            return True

        monkeypatch.setattr(livemap, "reachable", up)
        body = client.get(f"/api/instances/{inst['id']}/map").json()
        assert body["server_running"] and body["reachable"]
