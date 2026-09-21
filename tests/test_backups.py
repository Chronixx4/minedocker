"""Tests für Instanz-Backups: Modul-Logik + API-Routen."""
import io
import tarfile
import uuid

from app import backups as backups_mod
from app import instances as instances_mod


def _create_instance(name=None, **kwargs):
    kwargs.setdefault("loader", "fabric")
    kwargs.setdefault("game_version", "1.21.4")
    kwargs.setdefault("accept_eula", True)
    name = name or f"Backup-Test-{uuid.uuid4().hex[:8]}"
    return instances_mod.create_instance(name, **kwargs)


def _write_instance_data(instance_dir, content="welt-v1"):
    instance_dir.mkdir(parents=True, exist_ok=True)
    (instance_dir / "world").mkdir(exist_ok=True)
    (instance_dir / "world" / "level.dat").write_text(content)
    (instance_dir / "server.properties").write_text("motd=test\n")


class TestBackupModul:
    def test_create_und_list(self):
        inst = _create_instance()
        d = instances_mod.instance_dir(inst["id"])
        _write_instance_data(d)
        result = backups_mod.create_backup(inst["id"], d)
        assert result["name"].startswith("backup-") and result["name"].endswith(".tar.gz")
        assert result["size_bytes"] > 0
        liste = backups_mod.list_backups(inst["id"])
        assert len(liste) == 1
        assert liste[0]["name"] == result["name"]

    def test_restore_ersetzt_daten(self):
        inst = _create_instance()
        d = instances_mod.instance_dir(inst["id"])
        _write_instance_data(d)
        backup = backups_mod.create_backup(inst["id"], d)
        # Daten verändern (Welt v2)
        (d / "world" / "level.dat").write_text("welt-v2")
        backups_mod.restore_backup(inst["id"], backup["name"], d)
        assert (d / "world" / "level.dat").read_text() == "welt-v1"
        assert (d / "server.properties").read_text() == "motd=test\n"
        # Backup selbst bleibt erhalten
        assert len(backups_mod.list_backups(inst["id"])) == 1

    def test_delete_und_fehlender_backup(self):
        inst = _create_instance()
        d = instances_mod.instance_dir(inst["id"])
        _write_instance_data(d)
        backup = backups_mod.create_backup(inst["id"], d)
        backups_mod.delete_backup(inst["id"], backup["name"])
        assert backups_mod.list_backups(inst["id"]) == []
        try:
            backups_mod.backup_path(inst["id"], backup["name"])
            raise AssertionError("hätte FileNotFoundError werfen müssen")
        except FileNotFoundError:
            pass

    def test_ungueltiger_name_abgelehnt(self):
        inst = _create_instance()
        for name in ("../evil.tar.gz", "..\\evil.tar.gz", "backup.tar.gz.txt", ""):
            try:
                backups_mod.backup_path(inst["id"], name)
                raise AssertionError(f"'{name}' hätte abgelehnt werden müssen")
            except ValueError:
                pass
        except_file = False
        try:
            backups_mod.backup_path(inst["id"], "gibts-nicht-20260101-000000.tar.gz")
        except FileNotFoundError:
            except_file = True
        assert except_file


class TestSafetyBackup:
    def test_pre_install_schliesst_welt_und_packs_aus(self):
        inst = _create_instance()
        d = instances_mod.instance_dir(inst["id"])
        _write_instance_data(d)
        (d / "mods").mkdir(exist_ok=True)
        (d / "mods" / "altes-mod.jar").write_text("mod")
        (d / "packs").mkdir(exist_ok=True)
        (d / "packs" / "pack.mrpack").write_text("archiv")
        result = backups_mod.safety_backup(inst["id"], d, "pre-install")
        assert result["name"].startswith("pre-install-")
        with tarfile.open(backups_mod.backup_path(inst["id"], result["name"])) as tar:
            names = [m.name for m in tar.getmembers()]
        assert any("mods/altes-mod.jar" in n for n in names)
        assert any("instance.json" in n for n in names)
        assert not any("world" in n or "packs" in n for n in names)

    def test_pre_restore_behaelt_welt(self):
        inst = _create_instance()
        d = instances_mod.instance_dir(inst["id"])
        _write_instance_data(d)
        (d / "packs").mkdir(exist_ok=True)
        result = backups_mod.safety_backup(inst["id"], d, "pre-restore")
        assert result["name"].startswith("pre-restore-")
        with tarfile.open(backups_mod.backup_path(inst["id"], result["name"])) as tar:
            names = [m.name for m in tar.getmembers()]
        assert any("world/level.dat" in n for n in names)
        assert not any("packs" in n for n in names)

    def test_snapshot_ist_restorable(self):
        inst = _create_instance()
        d = instances_mod.instance_dir(inst["id"])
        _write_instance_data(d)
        (d / "mods").mkdir(exist_ok=True)
        (d / "mods" / "mod.jar").write_text("alt")
        safety = backups_mod.safety_backup(inst["id"], d, "pre-install")
        # Installation überschreibt die Mods
        (d / "mods" / "mod.jar").write_text("neu-überschrieben")
        backups_mod.restore_backup(inst["id"], safety["name"], d)
        assert (d / "mods" / "mod.jar").read_text() == "alt"

    def test_rotation_begisst_anzahl(self):
        import time as time_mod
        inst = _create_instance()
        d = instances_mod.instance_dir(inst["id"])
        _write_instance_data(d)
        for _ in range(5):
            result = backups_mod.safety_backup(inst["id"], d, "pre-install")
            (backups_mod.instance_backups_dir(inst["id"]) / result["name"]) \
                .touch()  # garantiert aufsteigende mtimes
            time_mod.sleep(0.01)
        liste = [b["name"] for b in backups_mod.list_backups(inst["id"])]
        safety = [n for n in liste if n.startswith("pre-install-")]
        assert len(safety) == 3

    def test_unbekannter_typ_abgelehnt(self):
        inst = _create_instance()
        d = instances_mod.instance_dir(inst["id"])
        try:
            backups_mod.safety_backup(inst["id"], d, "hakennetz")
            raise AssertionError("hätte ValueError werfen müssen")
        except ValueError:
            pass

    def test_restore_route_erstellt_pre_restore_snapshot(self, client):
        create = client.post("/api/instances", json={
            "name": "API-Safety", "loader": "fabric", "game_version": "1.21.4",
            "accept_eula": True})
        iid = create.json()["id"]
        d = instances_mod.instance_dir(iid)
        _write_instance_data(d)
        backup = backups_mod.create_backup(iid, d)
        (d / "world" / "level.dat").write_text("zerstört")
        r = client.post(f"/api/instances/{iid}/backups/{backup['name']}/restore")
        assert r.status_code == 200
        assert (d / "world" / "level.dat").read_text() == "welt-v1"
        names = [b["name"] for b in
                 client.get(f"/api/instances/{iid}/backups").json()["backups"]]
        assert any(n.startswith("pre-restore-") for n in names)


class TestBackupApi:
    def test_voller_zyklus(self, client):
        create = client.post("/api/instances", json={
            "name": "API-Backup", "loader": "fabric", "game_version": "1.21.4",
            "accept_eula": True})
        assert create.status_code == 201
        iid = create.json()["id"]

        # Backup erstellen
        r = client.post(f"/api/instances/{iid}/backups")
        assert r.status_code == 201
        name = r.json()["name"]
        assert name.endswith(".tar.gz")

        # Liste
        r = client.get(f"/api/instances/{iid}/backups")
        assert r.status_code == 200
        assert r.json()["backups"][0]["name"] == name

        # Download
        r = client.get(f"/api/instances/{iid}/backups/{name}/download")
        assert r.status_code == 200
        tar_data = r.content
        with tarfile.open(fileobj=io.BytesIO(tar_data), mode="r:gz") as tar:
            names = [m.name for m in tar.getmembers()]
            assert any("instance.json" in n for n in names)

        # Löschen
        r = client.delete(f"/api/instances/{iid}/backups/{name}")
        assert r.status_code == 200
        assert client.get(f"/api/instances/{iid}/backups").json()["backups"] == []

    def test_restore_route(self, client):
        create = client.post("/api/instances", json={
            "name": "API-Restore", "loader": "fabric", "game_version": "1.21.4",
            "accept_eula": True})
        iid = create.json()["id"]
        d = instances_mod.instance_dir(iid)
        _write_instance_data(d)
        backup = backups_mod.create_backup(iid, d)
        (d / "world" / "level.dat").write_text("zerstört")
        r = client.post(f"/api/instances/{iid}/backups/{backup['name']}/restore")
        assert r.status_code == 200
        assert (d / "world" / "level.dat").read_text() == "welt-v1"

    def test_unknown_instance_404(self, client):
        assert client.get("/api/instances/unbekannt/backups").status_code == 404
        assert client.post("/api/instances/unbekannt/backups").status_code == 404

    def test_restore_unbekanntes_backup_404(self, client):
        create = client.post("/api/instances", json={
            "name": "API-R404", "loader": "fabric", "game_version": "1.21.4",
            "accept_eula": True})
        iid = create.json()["id"]
        r = client.post(f"/api/instances/{iid}/backups/gibtsnicht-20260101-000000.tar.gz/restore")
        assert r.status_code == 404
