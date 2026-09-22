"""Tests für Datapacks pro Instanz (app/datapacks.py + Routen)."""
import shutil

import pytest
from fastapi import HTTPException

from app import datapacks as dp
from app import instances
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen():
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    yield
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


@pytest.fixture()
def instanz():
    return instances.create_instance("Datapack-Srv", "fabric", "1.21.4",
                                     accept_eula=True)


@pytest.fixture()
def welt_und_packs(instanz):
    """Welt-Ordner + datapacks-Ordner anlegen (wie nach erstem Serverstart)."""
    world = instances.instance_dir(instanz["id"]) / "world"
    world.mkdir(parents=True)
    (world / "level.dat").write_bytes(b"x")
    packs = world / "datapacks"
    packs.mkdir()
    return world, packs


class TestValidateName:
    @pytest.mark.parametrize("ok", ("mein-pack.zip", "Data_Pack.1.zip"))
    def test_gueltig(self, ok):
        assert dp.validate_name(ok) == ok

    @pytest.mark.parametrize("bad", ("", "../evil.zip", "kein-zip.jar",
                                     ".hidden.zip", "pfad/pack.zip", "a" * 130))
    def test_ungueltig(self, bad):
        with pytest.raises(HTTPException) as e:
            dp.validate_name(bad)
        assert e.value.status_code == 400


class TestListe:
    def test_ohne_welt_409(self, instanz):
        with pytest.raises(HTTPException) as e:
            dp.list_datapacks(instanz["id"])
        assert e.value.status_code == 409

    def test_aktiv_und_deaktiviert(self, instanz, welt_und_packs):
        _, packs = welt_und_packs
        (packs / "alpha.zip").write_bytes(b"PKa")
        disabled = packs / "disabled_datapacks"
        disabled.mkdir()
        (disabled / "beta.zip").write_bytes(b"PKb")
        (packs / "notiz.txt").write_text("ignoriert", encoding="utf-8")
        result = dp.list_datapacks(instanz["id"])
        by_name = {p["name"]: p for p in result["datapacks"]}
        assert by_name["alpha.zip"]["enabled"] is True
        assert by_name["beta.zip"]["enabled"] is False
        assert "notiz.txt" not in by_name


class TestUpload:
    def test_upload_und_kollision(self, instanz, welt_und_packs):
        result = dp.upload_datapack(instanz["id"], "neu.zip", b"PK")
        assert result["enabled"] is True
        with pytest.raises(HTTPException) as e:
            dp.upload_datapack(instanz["id"], "neu.zip", b"PK2")
        assert e.value.status_code == 409

    def test_upload_ohne_welt_409(self, instanz):
        with pytest.raises(HTTPException) as e:
            dp.upload_datapack(instanz["id"], "neu.zip", b"PK")
        assert e.value.status_code == 409


class TestEnableDisable:
    def test_enable_verschiebt_zurueck(self, instanz, welt_und_packs, monkeypatch):
        _, packs = welt_und_packs
        disabled = packs / "disabled_datapacks"
        disabled.mkdir()
        (disabled / "p.zip").write_bytes(b"PK")
        monkeypatch.setattr(dp, "runtime_is_running", lambda inst: False)
        result = dp.enable_datapack(instanz["id"], "p.zip")
        assert result == {"name": "p.zip", "enabled": True, "reloaded": None}
        assert (packs / "p.zip").is_file()
        # Aktivieren eines bereits aktiven Packs → 409
        with pytest.raises(HTTPException) as e:
            dp.enable_datapack(instanz["id"], "p.zip")
        assert e.value.status_code == 409

    def test_disable_verschiebt_und_rcon(self, instanz, welt_und_packs,
                                         monkeypatch):
        _, packs = welt_und_packs
        (packs / "p.zip").write_bytes(b"PK")
        calls = []
        monkeypatch.setattr(dp, "runtime_is_running", lambda inst: True)

        def fake_rcon(instance, command):
            calls.append(command)
            return "ok"

        monkeypatch.setattr(dp, "_rcon_datapack", fake_rcon)
        result = dp.disable_datapack(instanz["id"], "p.zip")
        assert result["enabled"] is False and result["reloaded"] is True
        assert calls == ['datapack disable "file/p.zip"']
        assert not (packs / "p.zip").exists()
        assert (packs / "disabled_datapacks" / "p.zip").is_file()

    def test_disable_rcon_fehler_fail_closed(self, instanz, welt_und_packs,
                                             monkeypatch):
        _, packs = welt_und_packs
        (packs / "p.zip").write_bytes(b"PK")
        monkeypatch.setattr(dp, "runtime_is_running", lambda inst: True)

        def boom(instance, command):
            raise HTTPException(status_code=503, detail="RCON weg")

        monkeypatch.setattr(dp, "_rcon_datapack", boom)
        with pytest.raises(HTTPException) as e:
            dp.disable_datapack(instanz["id"], "p.zip")
        assert e.value.status_code == 503
        # fail-closed: Datei unangetastet
        assert (packs / "p.zip").is_file()
        assert not (packs / "disabled_datapacks" / "p.zip").exists()


class TestDelete:
    def test_delete_gestoppt_aktiv_und_deaktiviert(self, instanz, welt_und_packs,
                                                   monkeypatch):
        _, packs = welt_und_packs
        disabled = packs / "disabled_datapacks"
        disabled.mkdir()
        (packs / "a.zip").write_bytes(b"PK")
        (disabled / "b.zip").write_bytes(b"PK")
        monkeypatch.setattr(dp, "runtime_is_running", lambda inst: False)
        assert dp.delete_datapack(instanz["id"], "a.zip", True)["deleted"] == "a.zip"
        assert dp.delete_datapack(instanz["id"], "b.zip",
                                  False)["deleted"] == "b.zip"
        assert not (packs / "a.zip").exists()
        assert not (disabled / "b.zip").exists()

    def test_delete_laufend_rcon_zuerst(self, instanz, welt_und_packs,
                                        monkeypatch):
        _, packs = welt_und_packs
        (packs / "a.zip").write_bytes(b"PK")
        calls = []
        monkeypatch.setattr(dp, "runtime_is_running", lambda inst: True)
        monkeypatch.setattr(dp, "_rcon_datapack",
                            lambda inst, cmd: calls.append(cmd) or "ok")
        dp.delete_datapack(instanz["id"], "a.zip", True)
        assert calls == ['datapack disable "file/a.zip"']
        assert not (packs / "a.zip").exists()

    def test_delete_laufend_rcon_fehler_nichts_geloescht(self, instanz,
                                                         welt_und_packs,
                                                         monkeypatch):
        _, packs = welt_und_packs
        (packs / "a.zip").write_bytes(b"PK")
        monkeypatch.setattr(dp, "runtime_is_running", lambda inst: True)

        def boom(instance, command):
            raise HTTPException(status_code=503, detail="RCON weg")

        monkeypatch.setattr(dp, "_rcon_datapack", boom)
        with pytest.raises(HTTPException) as e:
            dp.delete_datapack(instanz["id"], "a.zip", True)
        assert e.value.status_code == 503
        assert (packs / "a.zip").is_file()

    def test_delete_404(self, instanz, welt_und_packs, monkeypatch):
        monkeypatch.setattr(dp, "runtime_is_running", lambda inst: False)
        with pytest.raises(HTTPException) as e:
            dp.delete_datapack(instanz["id"], "fehlt.zip", True)
        assert e.value.status_code == 404


class TestRouten:
    def test_liste_upload_enable_disable_loeschen(self, client, instanz,
                                                  welt_und_packs, monkeypatch):
        base = f"/api/instances/{instanz['id']}/datapacks"
        monkeypatch.setattr(dp, "runtime_is_running", lambda inst: False)
        # Upload
        r = client.post(f"{base}/upload",
                        files={"file": ("meinpack.zip", b"PK", "application/zip")})
        assert r.status_code == 200
        assert r.json()["name"] == "meinpack.zip"
        # Liste zeigt es aktiv
        r = client.get(base)
        assert r.status_code == 200
        assert r.json()["datapacks"][0]["name"] == "meinpack.zip"
        # Disable (gestoppt → ohne RCON)
        r = client.post(f"{base}/disable", json={"name": "meinpack.zip"})
        assert r.status_code == 200
        r = client.get(base)
        assert r.json()["datapacks"][0]["enabled"] is False
        # Enable
        r = client.post(f"{base}/enable", json={"name": "meinpack.zip"})
        assert r.status_code == 200
        # Löschen
        r = client.delete(f"{base}/meinpack.zip", params={"enabled": "true"})
        assert r.status_code == 200
        assert client.get(base).json()["datapacks"] == []

    def test_upload_ungueltiger_name_400(self, client, instanz, welt_und_packs,
                                         monkeypatch):
        monkeypatch.setattr(dp, "runtime_is_running", lambda inst: False)
        r = client.post(
            f"/api/instances/{instanz['id']}/datapacks/upload",
            files={"file": ("evil.jar", b"PK", "application/java-archive")})
        assert r.status_code == 400

    def test_delete_ungueltiger_name_400(self, client, instanz, welt_und_packs):
        r = client.delete(f"/api/instances/{instanz['id']}/datapacks/evil.jar",
                          params={"enabled": "true"})
        assert r.status_code == 400

    def test_viewer_schreibgeschuetzt(self, client, instanz, welt_und_packs):
        client.post("/api/auth/setup", json={"username": "boss",
                                             "password": "passwort123"})
        client.post("/api/auth/users", json={"username": "leser",
                                             "password": "passwort123",
                                             "role": "viewer"})
        client.cookies.clear()
        client.post("/api/auth/login", json={"username": "leser",
                                             "password": "passwort123"})
        base = f"/api/instances/{instanz['id']}/datapacks"
        assert client.get(base).status_code == 200  # GET erlaubt
        assert client.post(f"{base}/enable",
                           json={"name": "x.zip"}).status_code == 403
        assert client.post(f"{base}/upload",
                           files={"file": ("x.zip", b"P", "application/zip")}
                           ).status_code == 403
