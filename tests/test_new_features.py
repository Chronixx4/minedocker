"""Tests für die neuen Features: server.properties-Formular-Editor (Schema +
Validierung), Tags/Gruppen mit Sammelaktionen, freie Portwahl (PATCH) und
Modpack-Update-Check/-Update in bestehende Instanzen."""
import asyncio
import concurrent.futures
import shutil

import httpx
import pytest
from fastapi import HTTPException

from app import instances, modrinth, packs, runtime
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen():
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    modrinth.JOBS.clear()
    modrinth._JOB_ORDER.clear()
    yield
    modrinth.JOBS.clear()
    modrinth._JOB_ORDER.clear()


@pytest.fixture()
def instanz():
    return instances.create_instance("Feature-Srv", "fabric", "1.21.4",
                                     accept_eula=True)


# ---------------------------------------------------------------------------
# Feature 1: server.properties-Formular-Editor (Schema + Validierung)
# ---------------------------------------------------------------------------

class TestPropsSchema:
    def test_schema_liegt_in_der_config_antwort(self, client, instanz):
        r = client.get(f"/api/instances/{instanz['id']}/config")
        assert r.status_code == 200
        schema = r.json()["schema"]
        by_key = {entry["key"]: entry for entry in schema}
        assert by_key["difficulty"]["type"] == "enum"
        assert "hard" in by_key["difficulty"]["choices"]
        assert by_key["view-distance"]["min"] == 3
        assert by_key["view-distance"]["max"] == 32
        assert by_key["online-mode"]["type"] == "bool"
        # Verwaltete Keys sind markiert
        assert by_key["server-port"]["managed"] is True
        assert by_key["rcon.password"]["managed"] is True

    def test_gueltige_werte_werden_normalisiert(self, instanz):
        count = instances.write_server_properties(instanz["id"], [
            {"key": "online-mode", "value": "TRUE"},
            {"key": "difficulty", "value": "Hard"},
            {"key": "view-distance", "value": " 12 "},
            {"key": "max-players", "value": "42"},
        ])
        assert count == 4
        values = {p["key"]: p["value"]
                  for p in instances.read_server_properties(instanz["id"])}
        assert values["online-mode"] == "true"
        assert values["difficulty"] == "hard"
        assert values["view-distance"] == "12"
        assert values["max-players"] == "42"

    def test_ungueltige_werte_abgelehnt(self, instanz):
        with pytest.raises(HTTPException) as exc:
            instances.write_server_properties(instanz["id"],
                                              [{"key": "difficulty", "value": "superhard"}])
        assert exc.value.status_code == 400
        with pytest.raises(HTTPException):
            instances.write_server_properties(instanz["id"],
                                              [{"key": "view-distance", "value": "99"}])
        with pytest.raises(HTTPException):
            instances.write_server_properties(instanz["id"],
                                              [{"key": "max-players", "value": "zwanzig"}])
        with pytest.raises(HTTPException):
            instances.write_server_properties(instanz["id"],
                                              [{"key": "pvp", "value": "maybe"}])

    def test_unbekannte_keys_bleiben_frei(self, instanz):
        count = instances.write_server_properties(instanz["id"], [
            {"key": "exotisch-key", "value": "Beliebig 123"},
        ])
        assert count == 1

    def test_verwaltete_keys_nur_intern_schreibbar(self, instanz):
        # API-Schreibpfad (managed_ok=False) zwingt verwaltete Keys auf den
        # Dateistand; fehlende verwaltete Keys werden nicht neu angelegt
        count = instances.write_server_properties(instanz["id"], [
            {"key": "level-name", "value": "eingeschmuggelt"},
            {"key": "server-port", "value": "1234"},
            {"key": "rcon.password", "value": "hacked"},
        ])
        values = {p["key"]: p["value"]
                  for p in instances.read_server_properties(instanz["id"])}
        assert count == 1  # nur motd-Vorlage etc. — server-port bewahrt, Rest nicht angelegt
        assert values["server-port"] == "25565"  # Dateistand bewahrt
        assert "level-name" not in values
        assert "rcon.password" not in values
        # Interne Aufrufer (Welt-Import/Restore) dürfen verwaltete Keys setzen
        count = instances.write_server_properties(
            instanz["id"], [{"key": "level-name", "value": "meine-welt"}],
            managed_ok=True)
        assert count == 1
        values = {p["key"]: p["value"]
                  for p in instances.read_server_properties(instanz["id"])}
        assert values["level-name"] == "meine-welt"

    def test_api_bewahrt_verwaltete_keys(self, client, instanz):
        r = client.post(f"/api/instances/{instanz['id']}/config",
                        json={"properties": [
                            {"key": "server-port", "value": "1234"},
                            {"key": "rcon.password", "value": "hacked"},
                            {"key": "motd", "value": "Ok"}]})
        assert r.status_code == 200
        values = {p["key"]: p["value"] for p in client.get(
            f"/api/instances/{instanz['id']}/config").json()["properties"]}
        assert values["server-port"] == "25565"  # verwaltet: unverändert
        assert "rcon.password" not in values
        assert values["motd"] == "Ok"

    def test_api_lehnt_ungueltigen_enum_wert_ab(self, client, instanz):
        r = client.post(f"/api/instances/{instanz['id']}/config",
                        json={"properties": [{"key": "gamemode", "value": "chaos"}]})
        assert r.status_code == 400
        assert "gamemode" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Feature 2: Tags/Gruppen + freie Portwahl
# ---------------------------------------------------------------------------

class TestTags:
    def test_erstellen_mit_tags(self, client):
        r = client.post("/api/instances", json={
            "name": "Tag-Srv", "loader": "fabric", "game_version": "1.21.4",
            "accept_eula": True, "tags": ["Survival", "Modded"],
        })
        assert r.status_code == 201
        assert r.json()["tags"] == ["Survival", "Modded"]

    def test_erstellen_ohne_tags_leer(self, instanz):
        assert instanz["tags"] == []

    def test_patch_tags_normalisiert_und_dedupliziert(self, client, instanz):
        r = client.patch(f"/api/instances/{instanz['id']}", json={
            "tags": ["Survival", " survival ", "Survival", "Modded", ""]})
        assert r.status_code == 200
        data = r.json()
        assert data["tags"] == ["Survival", "Modded"]

    def test_patch_tags_leere_liste_raeumt_auf(self, client, instanz):
        instances.update_settings(instanz["id"], tags=["A"])
        r = client.patch(f"/api/instances/{instanz['id']}", json={"tags": []})
        assert r.status_code == 200
        assert r.json()["tags"] == []

    def test_ungueltige_tags_400(self, client, instanz):
        r = client.patch(f"/api/instances/{instanz['id']}",
                         json={"tags": ["ungültig!"]})
        assert r.status_code == 400
        r = client.patch(f"/api/instances/{instanz['id']}",
                         json={"tags": [f"T{i}" for i in range(9)]})
        assert r.status_code == 400

    def test_tags_in_der_ubericht_liste(self, client, instanz):
        instances.update_settings(instanz["id"], tags=["GruppeX"])
        r = client.get("/api/instances")
        entry = next(i for i in r.json()["instances"] if i["id"] == instanz["id"])
        assert entry["tags"] == ["GruppeX"]

    def test_klon_ubernimmt_tags(self, instanz):
        instances.update_settings(instanz["id"], tags=["Vorlage"])
        clone = instances.clone_instance(instanz["id"])
        assert clone["tags"] == ["Vorlage"]


class TestPortWechsel:
    def test_patch_port_gestoppt(self, client, instanz, fake_docker):
        r = client.patch(f"/api/instances/{instanz['id']}", json={"port": 26000})
        assert r.status_code == 200
        data = r.json()
        assert data["port"] == 26000
        assert data["rcon_port"] == 27000

    def test_patch_port_gleich_ist_noop(self, client, instanz):
        r = client.patch(f"/api/instances/{instanz['id']}",
                         json={"port": instanz["port"]})
        assert r.status_code == 200

    def test_patch_port_kollision_409(self, client, instanz, fake_docker):
        other = instances.create_instance("Zweit-Srv", "fabric", "1.21.4",
                                          accept_eula=True)
        r = client.patch(f"/api/instances/{instanz['id']}",
                         json={"port": other["port"]})
        assert r.status_code == 409
        # Auch der RCON-Port (+1000) des anderen kollidiert
        r = client.patch(f"/api/instances/{instanz['id']}",
                         json={"port": other["port"] - 1000})
        assert r.status_code == 409

    def test_patch_port_laufend_409(self, client, instanz, monkeypatch):
        monkeypatch.setattr(runtime, "running_state", lambda inst: True)
        r = client.patch(f"/api/instances/{instanz['id']}", json={"port": 26100})
        assert r.status_code == 409

    def test_patch_port_status_unpruefbar_503(self, client, instanz, monkeypatch):
        """Fail-closed: Docker nicht erreichbar → Port-Wechsel abgelehnt."""
        monkeypatch.setattr(runtime, "running_state", lambda inst: None)
        r = client.patch(f"/api/instances/{instanz['id']}", json={"port": 26110})
        assert r.status_code == 503

    def test_patch_port_ausserhalb_bereichs_422(self, client, instanz):
        r = client.patch(f"/api/instances/{instanz['id']}", json={"port": 80})
        assert r.status_code == 422

    def test_port_im_start_verwendet(self, instanz, fake_docker):
        instances.update_settings(instanz["id"], port=26200)
        meta = instances.get_instance(instanz["id"])
        runtime.start_instance(meta)
        run_kwargs = fake_docker.containers.run_kwargs
        assert run_kwargs["ports"]["25565/tcp"] == 26200
        assert run_kwargs["ports"]["25575/tcp"] == 27200

    def test_race_portwechsel_atomar(self, instanz, fake_docker):
        """Zwei gleichzeitige Port-Änderungen erzeugen nie denselben Port
        (Validierung + Persistenz sind atomar)."""
        other = instances.create_instance("Dritt-Srv", "fabric", "1.21.4",
                                          accept_eula=True)
        target = other["port"]

        def try_swap_a():
            try:
                instances.update_settings(instanz["id"], port=target)
                return "ok"
            except HTTPException as exc:
                return exc.status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(try_swap_a)
            # B weicht auf einen freien Port aus (nicht A-Ursprungsport 25570)
            f2 = pool.submit(instances.update_settings, other["id"], port=27000)
            result_a = f1.result()
            f2.result()

        ports = [instances.get_instance(instanz["id"])["port"],
                 instances.get_instance(other["id"])["port"]]
        # Nie denselben Port — sonst Bind-Konflikt beim nächsten Start
        assert len(ports) == len(set(ports))
        # Verlierer wurde sauber abgelehnt (409), nie stillschweigend zugelassen
        if result_a != "ok":
            assert result_a == 409


# ---------------------------------------------------------------------------
# Feature 3: Modpack-Update-Check + Pack-Update
# ---------------------------------------------------------------------------

def _set_modpack(inst_id: str, mp: dict) -> None:
    inst = instances.get_instance(inst_id)
    inst["modpack"] = mp
    instances.update_instance(inst)


class TestPackUpdateCheck:
    def test_ohne_modpack(self, client, instanz):
        r = client.get(f"/api/instances/{instanz['id']}/modpacks/update-check")
        assert r.status_code == 200
        data = r.json()
        assert data["installed"] is False
        assert data["checkable"] is False

    def test_upload_ohne_quelle_nicht_pruefbar(self, client, instanz):
        _set_modpack(instanz["id"], {"title": "Upload", "source": "upload",
                                     "version_id": None, "project_id": None})
        r = client.get(f"/api/instances/{instanz['id']}/modpacks/update-check")
        assert r.status_code == 200
        data = r.json()
        assert data["installed"] is True
        assert data["checkable"] is False
        assert data["reason"]

    def test_modrinth_update_verfuegbar(self, client, instanz, monkeypatch):
        _set_modpack(instanz["id"], {"project_id": "packproj", "version_id": "v1",
                                     "title": "Pack", "source": "modrinth"})
        versions = [
            {"id": "v3", "name": "Pack v3", "date_published": "2026-02-01T00:00:00Z",
             "loaders": ["fabric"], "game_versions": ["1.21.4"],
             "files": [{"filename": "v3.mrpack", "url": "https://cdn.example/v3.mrpack",
                        "size": 10, "primary": True}]},
            {"id": "v2", "name": "Pack v2", "date_published": "2026-01-01T00:00:00Z",
             "loaders": ["fabric"], "game_versions": ["1.21.4"],
             "files": [{"filename": "v2.mrpack", "url": "https://cdn.example/v2.mrpack",
                        "size": 9, "primary": True}]},
            {"id": "v1", "name": "Pack v1", "date_published": "2025-12-01T00:00:00Z",
             "loaders": ["fabric"], "game_versions": ["1.21.4"],
             "files": [{"filename": "v1.mrpack", "url": "https://cdn.example/v1.mrpack",
                        "size": 8, "primary": True}]},
            # Inkompatible neuere Version (anderer Loader) darf nicht zählen
            {"id": "v9", "name": "Pack v9", "date_published": "2026-03-01T00:00:00Z",
             "loaders": ["forge"], "game_versions": ["1.21.4"],
             "files": [{"filename": "v9.mrpack", "url": "https://cdn.example/v9.mrpack",
                        "size": 7, "primary": True}]},
        ]

        def factory(**kwargs):
            def handler(request: httpx.Request) -> httpx.Response:
                assert request.url.path.endswith("/project/packproj/version")
                return httpx.Response(200, json=versions)
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(packs, "_new_client", factory)
        r = client.get(f"/api/instances/{instanz['id']}/modpacks/update-check")
        assert r.status_code == 200
        data = r.json()
        assert data["checkable"] is True
        assert data["update_available"] is True
        assert data["latest"]["version_id"] == "v3"
        assert data["installed_pack"]["version_id"] == "v1"
        assert data["compatible"] is True

    def test_modrinth_aktuell(self, client, instanz, monkeypatch):
        _set_modpack(instanz["id"], {"project_id": "packproj", "version_id": "v3",
                                     "title": "Pack"})
        versions = [{"id": "v3", "name": "Pack v3",
                     "date_published": "2026-02-01T00:00:00Z",
                     "loaders": ["fabric"], "game_versions": ["1.21.4"],
                     "files": []}]

        def factory(**kwargs):
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=versions)
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(packs, "_new_client", factory)
        data = client.get(
            f"/api/instances/{instanz['id']}/modpacks/update-check").json()
        assert data["update_available"] is False

    def test_curseforge_ohne_key_503(self, client, instanz):
        _set_modpack(instanz["id"], {"project_id": "somepack", "version_id": "123",
                                     "source": "curseforge", "title": "CF-Pack"})
        r = client.get(f"/api/instances/{instanz['id']}/modpacks/update-check")
        assert r.status_code == 503

    def test_curseforge_update_verfuegbar(self, client, instanz, monkeypatch):
        _set_modpack(instanz["id"], {"project_id": "cfpack", "version_id": "100",
                                     "source": "curseforge", "title": "CF-Pack"})
        monkeypatch.setattr(settings, "cf_api_key", "test-key")
        files = [
            {"id": 300, "fileName": "pack-3.zip", "fileDate": "2026-02-01",
             "fileSize": 30, "releaseType": 1, "gameVersions": ["1.21.4", "Fabric"]},
            {"id": 200, "fileName": "pack-2.zip", "fileDate": "2026-01-01",
             "fileSize": 20, "releaseType": 1, "gameVersions": ["1.21.4", "Fabric"]},
            {"id": 100, "fileName": "pack-1.zip", "fileDate": "2025-12-01",
             "fileSize": 10, "releaseType": 1, "gameVersions": ["1.21.4", "Fabric"]},
        ]

        def factory(**kwargs):
            def handler(request: httpx.Request) -> httpx.Response:
                path = request.url.path
                if path.endswith("/files"):
                    return httpx.Response(200, json={"data": files})
                if path.endswith("/search"):  # Slug-Auflösung (_resolve_project)
                    return httpx.Response(200, json={"data": [
                        {"id": 42, "slug": "cfpack", "name": "CF-Pack"}]})
                return httpx.Response(200, json={"data": {"id": 42, "slug": "cfpack",
                                                          "name": "CF-Pack"}})
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(packs, "_new_client", factory)
        import app.curseforge as cf
        monkeypatch.setattr(cf, "_new_client", factory)
        r = client.get(f"/api/instances/{instanz['id']}/modpacks/update-check")
        assert r.status_code == 200
        data = r.json()
        assert data["update_available"] is True
        assert data["latest"]["version_id"] == "300"
        assert data["compatible"] is True

    def test_curseforge_ohne_versions_id_pinnt_beim_update(self, client, instanz,
                                                           monkeypatch):
        """Ältere CF-Installationen ohne persistierte Datei-ID: statt des
        Dauer-Fehlalarms wird einmal „aktualisieren“ empfohlen (pinnt die ID)."""
        _set_modpack(instanz["id"], {"project_id": "cfpack", "version_id": None,
                                     "source": "curseforge", "title": "CF-Pack"})
        monkeypatch.setattr(settings, "cf_api_key", "test-key")
        files = [
            {"id": 300, "fileName": "pack-3.zip", "fileDate": "2026-02-01",
             "fileSize": 30, "releaseType": 1, "gameVersions": ["1.21.4", "Fabric"]},
        ]

        def factory(**kwargs):
            def handler(request: httpx.Request) -> httpx.Response:
                path = request.url.path
                if path.endswith("/files"):
                    return httpx.Response(200, json={"data": files})
                if path.endswith("/search"):
                    return httpx.Response(200, json={"data": [
                        {"id": 42, "slug": "cfpack", "name": "CF-Pack"}]})
                return httpx.Response(200, json={"data": {"id": 42, "slug": "cfpack",
                                                          "name": "CF-Pack"}})
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(packs, "_new_client", factory)
        import app.curseforge as cf
        monkeypatch.setattr(cf, "_new_client", factory)
        data = client.get(
            f"/api/instances/{instanz['id']}/modpacks/update-check").json()
        assert data["checkable"] is True
        assert data["update_available"] is True  # einmal aktualisieren → ID gepinnt
        assert data["latest"]["version_id"] == "300"

    def test_curseforge_inkompatibel_neueste(self, client, instanz, monkeypatch):
        _set_modpack(instanz["id"], {"project_id": "cfpack", "version_id": "100",
                                     "source": "curseforge", "title": "CF-Pack"})
        monkeypatch.setattr(settings, "cf_api_key", "test-key")
        files = [
            {"id": 300, "fileName": "pack-3.zip", "fileDate": "2026-02-01",
             "fileSize": 30, "releaseType": 1, "gameVersions": ["1.22.0", "Forge"]},
            {"id": 100, "fileName": "pack-1.zip", "fileDate": "2025-12-01",
             "fileSize": 10, "releaseType": 1, "gameVersions": ["1.21.4", "Fabric"]},
        ]

        def factory(**kwargs):
            def handler(request: httpx.Request) -> httpx.Response:
                path = request.url.path
                if path.endswith("/files"):
                    return httpx.Response(200, json={"data": files})
                if path.endswith("/search"):
                    return httpx.Response(200, json={"data": [
                        {"id": 42, "slug": "cfpack", "name": "CF-Pack"}]})
                return httpx.Response(200, json={"data": {"id": 42, "slug": "cfpack",
                                                          "name": "CF-Pack"}})
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(packs, "_new_client", factory)
        import app.curseforge as cf
        monkeypatch.setattr(cf, "_new_client", factory)
        data = client.get(
            f"/api/instances/{instanz['id']}/modpacks/update-check").json()
        assert data["compatible"] is False
        assert data["update_available"] is False
        assert data["latest"]["version_id"] == "300"


class TestPackUpdate:
    def test_ohne_modpack_400(self, client, instanz):
        r = client.post(f"/api/instances/{instanz['id']}/modpacks/update", json={})
        assert r.status_code == 400

    def test_upload_ohne_quelle_400(self, client, instanz):
        _set_modpack(instanz["id"], {"title": "Upload", "source": "upload",
                                     "project_id": None})
        r = client.post(f"/api/instances/{instanz['id']}/modpacks/update", json={})
        assert r.status_code == 400

    def test_modrinth_update_startet_install_mit_force(self, client, instanz,
                                                       monkeypatch):
        _set_modpack(instanz["id"], {"project_id": "packproj", "version_id": "v1",
                                     "title": "Pack"})
        calls = {}

        async def fake_install(inst_id, project_id, version_id=None, force=False):
            calls.update({"instance": inst_id, "project": project_id,
                          "version_id": version_id, "force": force})
            return {"id": "job123", "filename": "pack.mrpack", "total": 10,
                    "phase": "Modpack-Download"}

        monkeypatch.setattr(packs, "install_pack", fake_install)
        r = client.post(f"/api/instances/{instanz['id']}/modpacks/update", json={})
        assert r.status_code == 200
        assert r.json()["job_id"] == "job123"
        assert calls == {"instance": instanz["id"], "project": "packproj",
                         "version_id": None, "force": True}

    def test_modrinth_update_explizite_version(self, client, instanz, monkeypatch):
        _set_modpack(instanz["id"], {"project_id": "packproj", "version_id": "v1",
                                     "title": "Pack"})
        calls = {}

        async def fake_install(inst_id, project_id, version_id=None, force=False):
            calls.update({"version_id": version_id, "force": force})
            return {"id": "job1", "filename": "f", "total": 1, "phase": "x"}

        monkeypatch.setattr(packs, "install_pack", fake_install)
        r = client.post(f"/api/instances/{instanz['id']}/modpacks/update",
                        json={"version_id": "v2"})
        assert r.status_code == 200
        assert calls == {"version_id": "v2", "force": True}

    def test_cf_update_startet_install_cf(self, client, instanz, monkeypatch):
        _set_modpack(instanz["id"], {"project_id": "cfpack", "version_id": "100",
                                     "source": "curseforge", "title": "CF-Pack"})
        monkeypatch.setattr(settings, "cf_api_key", "test-key")
        calls = {}

        async def fake_install_cf(inst_id, project_id, file_id=None, force=False):
            calls.update({"file_id": file_id, "force": force})
            return {"id": "job2", "filename": "pack.zip", "total": 0,
                    "phase": "Modpack-Download"}

        import app.curseforge as cf
        monkeypatch.setattr(cf, "_require_key", lambda: "test-key")
        monkeypatch.setattr(packs, "install_pack_cf", fake_install_cf)
        r = client.post(f"/api/instances/{instanz['id']}/modpacks/update",
                        json={"file_id": "300"})
        assert r.status_code == 200
        assert calls == {"file_id": "300", "force": True}

    def test_ende_bis_ende_job_modrinth(self, instanz, monkeypatch):
        """Vollständiger Durchlauf (Direktaufruf, wie die Pack-Tests):
        Check → Update-Job läuft durch → Meta auf neue Version aktualisiert."""
        _set_modpack(instanz["id"], {"project_id": "packproj", "version_id": "v1",
                                     "title": "Pack"})
        pack_bytes = _mrpack_for_tests()
        versions = [
            {"id": "v2", "name": "Pack v2", "date_published": "2026-02-01T00:00:00Z",
             "loaders": ["fabric"], "game_versions": ["1.21.4"],
             "files": [{"filename": "v2.mrpack", "url": "https://cdn.example/v2.mrpack",
                        "size": len(pack_bytes), "primary": True}]},
            {"id": "v1", "name": "Pack v1", "date_published": "2025-12-01T00:00:00Z",
             "loaders": ["fabric"], "game_versions": ["1.21.4"],
             "files": [{"filename": "v1.mrpack", "url": "https://cdn.example/v1.mrpack",
                        "size": len(pack_bytes), "primary": True}]},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/version"):
                return httpx.Response(200, json=versions)
            if path.endswith("/v2.mrpack"):
                return httpx.Response(200, content=pack_bytes)
            if path.endswith("/mod.jar"):
                return httpx.Response(200, content=b"fake-jar" * 10)
            return httpx.Response(404, content=b"?")

        real_client = httpx.AsyncClient

        def factory(**kwargs):
            kwargs.pop("transport", None)
            kwargs.setdefault("follow_redirects", True)
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        monkeypatch.setattr(packs, "_new_client", factory)

        async def _flow():
            job = await packs.pack_update(instanz["id"], None, None)
            for _ in range(500):
                if job["status"] in ("done", "error"):
                    return job
                await asyncio.sleep(0.02)
            return job

        job = asyncio.run(_flow())
        assert job["status"] == "done", job.get("error")
        meta = instances.get_instance(instanz["id"])
        assert meta["modpack"]["version_id"] == "v2"
        assert meta["modpack"]["project_id"] == "packproj"


def _mrpack_for_tests() -> bytes:
    import hashlib
    import io
    import json
    import zipfile
    jar = b"fake-jar" * 10
    index = {
        "formatVersion": 1,
        "gameVersion": "1.21.4",
        "dependencies": {"fabric-loader": "0.16.9"},
        "files": [
            {"path": "mods/mod.jar", "downloads": ["https://cdn.example/mod.jar"],
             "fileSize": len(jar), "hashes": {"sha1": hashlib.sha1(jar).hexdigest()}},
        ],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("modrinth.index.json", json.dumps(index))
    return buf.getvalue()


def _wait_job(job_id: str) -> dict:
    async def _flow():
        for _ in range(500):
            job = modrinth.get_job(job_id)
            if job and job["status"] in ("done", "error"):
                return job
            await asyncio.sleep(0.02)
        return modrinth.get_job(job_id)

    return asyncio.run(_flow())
