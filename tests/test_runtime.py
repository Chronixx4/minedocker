"""Tests für app.runtime (Container-Start/Stop) mit Fake-Docker-Client."""
import shutil

import pytest

from app import instances, runtime
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen():
    """Saubere Instanz-Umgebung pro Test (verhindert Namens-Kollisionen)."""
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    yield


@pytest.fixture()
def instanz():
    return instances.create_instance("Run-Srv", "fabric", "1.21.4",
                                     loader_version="0.16.9", accept_eula=True)


def _env_dict(instance):
    return dict(item.split("=", 1) for item in runtime._env(instance))


class TestEnvMapping:
    def test_fabric(self):
        env = _env_dict({"loader": "fabric", "game_version": "1.21.4",
                         "name": "X", "memory": "2G", "loader_version": "0.16.9"})
        assert env["TYPE"] == "FABRIC"
        assert env["VERSION"] == "1.21.4"
        assert env["LOADER_VERSION"] == "0.16.9"
        assert env["EULA"] == "TRUE"
        assert env["ENABLE_RCON"] == "TRUE"
        assert env["RCON_PORT"] == "25575"
        assert len(env["RCON_PASSWORD"]) == 24  # deterministisch aus Salt + ID
        assert env["RCON_PASSWORD"] == _env_dict(
            {"loader": "fabric", "game_version": "1.21.4",
             "name": "X", "memory": "2G"})["RCON_PASSWORD"]
        assert env["MEMORY"] == "2G"

    @pytest.mark.parametrize("loader,typ,env_key", [
        ("forge", "FORGE", "FORGE_VERSION"),
        ("neoforge", "NEOFORGE", "NEOFORGE_VERSION"),
        ("quilt", "QUILT", "LOADER_VERSION"),
        ("paper", "PAPER", "PAPER_BUILD"),
        ("bukkit", "BUKKIT", None),
    ])
    def test_loader_typen(self, loader, typ, env_key):
        inst = {"loader": loader, "game_version": "1.20.1",
                "name": "X", "memory": None, "loader_version": "9.9"}
        env = _env_dict(inst)
        assert env["TYPE"] == typ
        if env_key:
            assert env[env_key] == "9.9"
        else:
            assert "LOADER_VERSION" not in env

    def test_memory_default(self):
        env = _env_dict({"loader": "fabric", "game_version": "1.21.4",
                         "name": "X", "memory": None, "loader_version": None})
        assert env["MEMORY"] == settings.instances_memory


class TestStartStop:
    def test_start_erstellt_container(self, fake_docker, instanz):
        runtime.start_instance(instanz)
        cname = runtime.container_name(instanz["id"])
        assert cname in fake_docker.containers._items
        kwargs = fake_docker.containers.run_kwargs
        assert kwargs["image"] == runtime.IMAGE
        assert kwargs["ports"] == {"25565/tcp": instanz["port"],
                                   "25575/tcp": instanz["rcon_port"]}
        assert kwargs["labels"]["mc-dashboard.instance"] == instanz["id"]
        volumes = kwargs["volumes"]
        bind = next(iter(volumes.values()))
        assert bind == {"bind": "/data", "mode": "rw"}
        assert next(iter(volumes)).endswith(instanz["id"])

    def test_start_laeuft_bereits(self, fake_docker, instanz):
        runtime.start_instance(instanz)
        with pytest.raises(RuntimeError, match="läuft bereits"):
            runtime.start_instance(instanz)

    def test_stop_und_status(self, fake_docker, instanz):
        runtime.start_instance(instanz)
        assert runtime.is_running(instanz) is True
        runtime.stop_instance(instanz)
        assert runtime.is_running(instanz) is False
        status = runtime.container_status(instanz)
        assert status["running"] is False
        assert status["error"] is None

    def test_status_started_at(self, fake_docker, instanz):
        # Laufender Container liefert die Startzeit (Nanosekunden gekürzt,
        # damit das Frontend sie mit new Date() parsen kann)
        assert runtime.container_status(instanz)["started_at"] is None
        runtime.start_instance(instanz)
        status = runtime.container_status(instanz)
        assert status["started_at"] == "2026-01-01T12:00:00.123Z"

    def test_ohne_container(self, fake_docker, instanz):
        assert runtime.is_running(instanz) is False
        assert runtime.container_status(instanz)["running"] is False
        runtime.stop_instance(instanz)  # kein Fehler
        runtime.remove_container(instanz)  # kein Fehler

    def test_remove_container(self, fake_docker, instanz):
        runtime.start_instance(instanz)
        runtime.remove_container(instanz)
        assert fake_docker.containers._items[runtime.container_name(instanz["id"])].removed


class TestLogs:
    def test_logs_mit_tail(self, fake_docker, instanz):
        runtime.start_instance(instanz)
        container = fake_docker.containers._items[runtime.container_name(instanz["id"])]
        container._logs = b"Zeile1\nZeile2\nZeile3"
        assert runtime.logs(instanz, tail=2) == ["Zeile2", "Zeile3"]


class TestRcon:
    def test_rcon_port_und_fallback(self):
        assert runtime.rcon_port({"port": 25570}) == 26570
        assert runtime.rcon_port({"port": 25570, "rcon_port": 26600}) == 26600

    def test_rcon_secret_stabil_und_je_instanz(self):
        a1 = runtime.rcon_secret({"id": "abc12345"})
        a2 = runtime.rcon_secret({"id": "abc12345"})
        b = runtime.rcon_secret({"id": "zzzz9999"})
        assert a1 == a2
        assert a1 != b
        assert len(a1) == 24

    def test_parse_list_output(self):
        from app import rcon as rcon_mod
        zero = rcon_mod.parse_list_output("There are 0 of a max of 20 players online")
        assert zero == {"online": 0, "max": 20, "names": []}
        two = rcon_mod.parse_list_output(
            "There are 2 of a max of 20 players online: Steve, Alex")
        assert two == {"online": 2, "max": 20, "names": ["Steve", "Alex"]}
        assert rcon_mod.parse_list_output("kaputt") is None

    def test_logs_ohne_container_leer(self, fake_docker, instanz):
        assert runtime.logs(instanz) == []


class TestCpuStats:
    @staticmethod
    def _stats(cpu_ns, system_ns, online_cpus=8):
        return {
            "cpu_stats": {"cpu_usage": {"total_usage": cpu_ns},
                          "system_cpu_usage": system_ns,
                          "online_cpus": online_cpus},
            "precpu_stats": {"cpu_usage": {"total_usage": 0},
                             "system_cpu_usage": 0},
        }

    def test_mehrkern_auslastung_bleibt_unter_100(self):
        """7 von 8 Kernen belegt: Docker-Rohwert wäre 700 % — normalisiert 87.5 %."""
        # cpu_delta 7e9 ns, system_delta über alle 8 Kerne 8e9 ns
        pct = runtime._cpu_percent(self._stats(7 * 10**9, 8 * 10**9,
                                               online_cpus=8))
        assert pct == 87.5

    def test_anderer_kernzahl_gleiches_ergebnis(self):
        """Die Normalisierung hängt nicht von online_cpus ab (System-Tick
        summiert über alle Kerne)."""
        assert runtime._cpu_percent(self._stats(7 * 10**9, 8 * 10**9,
                                                online_cpus=4)) == 87.5

    def test_ein_kern_voll(self):
        assert runtime._cpu_percent(self._stats(10**9, 8 * 10**9)) == 12.5

    def test_leer_und_fehler_hartnaeckig_null(self):
        assert runtime._cpu_percent({}) == 0.0
        assert runtime._cpu_percent({"cpu_stats": {}, "precpu_stats": {}}) == 0.0
        # System-Tick 0 (erster Aufruf) → 0 statt Division durch 0
        assert runtime._cpu_percent(
            {"cpu_stats": {"cpu_usage": {"total_usage": 100},
                           "system_cpu_usage": 0},
             "precpu_stats": {"cpu_usage": {"total_usage": 0},
                              "system_cpu_usage": 0}}) == 0.0


class TestSonstiges:
    def test_container_name(self):
        assert runtime.container_name("abcd1234") == "mc-inst-abcd1234"

    def test_host_pfad_aus_env(self):
        assert runtime._host_instances_root() == settings.instances_host_dir

    def test_env_baustein_reihenfolge_stabil(self):
        env = runtime._env({"loader": "fabric", "game_version": "1.21.4",
                            "name": "X", "memory": "4G", "loader_version": None})
        assert env[0] == "EULA=TRUE"
