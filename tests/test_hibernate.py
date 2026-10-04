"""Tests für den Schlafmodus (itzg Autopause/Autostop)."""
import shutil

import pytest
from fastapi import HTTPException

from app import backups, history, instances, runtime, scheduler
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen():
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    yield


@pytest.fixture()
def instanz():
    return instances.create_instance("Schlaf-Srv", "paper", "1.21.4", accept_eula=True)


def _env(instance):
    return dict(item.split("=", 1) for item in runtime._env(instance))


class TestEinstellungen:
    def test_standard_aus(self, instanz):
        assert instances.hibernate_settings(instanz) == {"mode": "off", "minutes": 30}
        env = _env(instanz)
        assert "ENABLE_AUTOPAUSE" not in env and "ENABLE_AUTOSTOP" not in env

    def test_speichern_und_validieren(self, instanz):
        result = instances.update_settings(instanz["id"],
                                           hibernate={"mode": "pause", "minutes": 15})
        assert result["instance"]["hibernate"] == {"mode": "pause", "minutes": 15}
        assert "hibernate" in result["changed"]
        for bad in ({"mode": "schlafen"}, {"mode": "stop", "minutes": 1},
                    {"mode": "stop", "minutes": "x"}, "pause"):
            with pytest.raises(HTTPException) as exc:
                instances.update_settings(instanz["id"], hibernate=bad)
            assert exc.value.status_code == 400

    def test_kaputter_wert_gilt_als_aus(self):
        assert instances.hibernate_settings({"hibernate": {"mode": "?"}})["mode"] == "off"

    def test_api_patch(self, client, instanz):
        r = client.patch(f"/api/instances/{instanz['id']}",
                         json={"hibernate": {"mode": "stop", "minutes": 60}})
        assert r.status_code == 200, r.text
        assert r.json()["hibernate"] == {"mode": "stop", "minutes": 60}
        r = client.patch(f"/api/instances/{instanz['id']}",
                         json={"hibernate": {"mode": "nie"}})
        assert r.status_code == 422


class TestContainer:
    def test_pause_env_und_ohne_no_new_privileges(self, fake_docker, instanz):
        instances.update_settings(instanz["id"], hibernate={"mode": "pause", "minutes": 10})
        inst = instances.get_instance(instanz["id"])
        env = _env(inst)
        assert env["ENABLE_AUTOPAUSE"] == "TRUE"
        assert env["AUTOPAUSE_TIMEOUT_EST"] == "600"
        assert env["MAX_TICK_TIME"] == "-1"
        assert env["JVM_DD_OPTS"] == "disable.watchdog:true"  # Paper
        runtime.start_instance(inst)
        kwargs = fake_docker.containers.run_kwargs
        assert kwargs["security_opt"] == []
        assert kwargs["restart_policy"] == {"Name": "unless-stopped"}

    def test_stop_env_und_restart_policy(self, fake_docker, instanz):
        instances.update_settings(instanz["id"], hibernate={"mode": "stop", "minutes": 45})
        inst = instances.get_instance(instanz["id"])
        env = _env(inst)
        assert env["ENABLE_AUTOSTOP"] == "TRUE"
        assert env["AUTOSTOP_TIMEOUT_EST"] == "2700"
        runtime.start_instance(inst)
        kwargs = fake_docker.containers.run_kwargs
        assert kwargs["restart_policy"]["Name"] == "on-failure"
        assert kwargs["security_opt"] == ["no-new-privileges:true"]

    def test_fabric_ohne_paper_watchdog_flag(self):
        env = _env({"id": "x", "loader": "fabric", "game_version": "1.21.4", "name": "X",
                    "hibernate": {"mode": "pause", "minutes": 30}})
        assert "JVM_DD_OPTS" not in env

    def test_start_entfernt_alte_markierung(self, fake_docker, instanz):
        flag = instances.instance_dir(instanz["id"]) / instances.PAUSED_FLAG
        flag.touch()
        runtime.start_instance(instanz)
        assert not flag.exists()

    def test_stop_weckt_vorher_auf(self, fake_docker, instanz):
        runtime.start_instance(instanz)
        flag = instances.instance_dir(instanz["id"]) / instances.PAUSED_FLAG
        flag.touch()
        assert instances.is_paused(instanz["id"])
        runtime.stop_instance(instanz)
        container = fake_docker.containers._items[runtime.container_name(instanz["id"])]
        assert container.execs == [["pkill", "-CONT", "java"]]
        assert not flag.exists()


class TestNichtWecken:
    def _schlafen(self, instanz):
        (instances.instance_dir(instanz["id"]) / instances.PAUSED_FLAG).touch()

    def test_details_ohne_ping(self, client, fake_docker, instanz, monkeypatch):
        runtime.start_instance(instanz)
        self._schlafen(instanz)
        from app import main
        monkeypatch.setattr(main, "server_status",
                            lambda *a, **k: pytest.fail("Ping weckt den Server"))
        detail = client.get(f"/api/instances/{instanz['id']}").json()
        assert detail["container"]["paused"] is True
        live = client.get("/api/instances/live").json()["live"]
        assert live[0]["paused"] is True
        listing = client.get("/api/instances").json()["instances"]
        assert listing[0]["container"]["paused"] is True

    def test_sampler_pingt_nicht(self, fake_docker, instanz, monkeypatch):
        self._schlafen(instanz)
        monkeypatch.setattr(runtime, "docker_resources", lambda: {"containers": [
            {"name": runtime.container_name(instanz["id"]), "cpu_percent": 0, "ram_mb": 1}]})
        monkeypatch.setattr(history, "_ping_players",
                            lambda *a: pytest.fail("Ping weckt den Server"))
        monkeypatch.setattr(history, "record_samples", lambda rows: None)
        assert history.sample_tick(1_800_000_000) == 2

    def test_vorwarnung_und_backup_ohne_rcon(self, instanz, monkeypatch):
        self._schlafen(instanz)
        from app import rcon
        monkeypatch.setattr(rcon, "command", lambda *a, **k: pytest.fail("RCON weckt"))
        assert scheduler._rcon_say(instanz, "Neustart") is False
        monkeypatch.setattr(runtime, "is_running", lambda inst: True)
        with backups.world_flushed(instanz["id"]):
            pass
