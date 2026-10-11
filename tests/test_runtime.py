"""Tests für app.runtime (Container-Start/Stop) mit Fake-Docker-Client."""
import shutil
import socket

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


class TestJavaVersion:
    @pytest.mark.parametrize("version,tag", [
        ("1.7.10", "java8"),
        ("1.12.2", "java8"),
        ("1.16.5", "java8"),
        ("1.17.1", "java17"),
        ("1.18", "java17"),
        ("1.20.4", "java17"),
        ("1.20.5", "java21"),
        ("1.21.4", "java21"),
        ("26.1", "latest"),
        ("25w14a", "latest"),
        ("", "latest"),
    ])
    def test_automatisch_nach_mc_version(self, version, tag):
        assert runtime.java_tag({"game_version": version}) == tag

    def test_wahl_je_instanz_hat_vorrang(self):
        assert runtime.java_tag({"game_version": "1.12.2", "java": "11"}) == "java11"
        assert runtime.java_tag({"game_version": "1.12.2", "java": "auto"}) == "java8"
        assert runtime.java_tag({"game_version": "1.12.2", "java": "kaputt"}) == "java8"

    def test_start_nutzt_gewaehlte_java_version(self, fake_docker, instanz):
        instances.update_settings(instanz["id"], java="25")
        runtime.start_instance(instances.get_instance(instanz["id"]))
        assert fake_docker.containers.run_kwargs["image"] == "itzg/minecraft-server:java25"

    def test_ungueltige_java_version_abgelehnt(self, instanz):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            instances.update_settings(instanz["id"], java="7")
        assert exc.value.status_code == 400


class TestStartStop:
    def test_start_erstellt_container(self, fake_docker, instanz):
        runtime.start_instance(instanz)
        cname = runtime.container_name(instanz["id"])
        assert cname in fake_docker.containers._items
        kwargs = fake_docker.containers.run_kwargs
        assert kwargs["image"] == "itzg/minecraft-server:java21"  # MC 1.21.4
        # Nur der Spiel-Port ist offen; RCON bleibt auf dem Host lokal
        assert kwargs["ports"] == {"25565/tcp": instanz["port"],
                                   "25575/tcp": ("127.0.0.1", instanz["rcon_port"])}
        assert kwargs["labels"]["mc-dashboard.instance"] == instanz["id"]
        volumes = kwargs["volumes"]
        bind = next(iter(volumes.values()))
        assert bind == {"bind": "/data", "mode": "rw"}
        assert next(iter(volumes)).endswith(instanz["id"])

    def test_start_ohne_gpu_setzt_keine_nvidia_optionen(self, fake_docker, instanz):
        runtime.start_instance(instanz)
        kwargs = fake_docker.containers.run_kwargs
        assert "runtime" not in kwargs
        assert not any(e.startswith("NVIDIA_") for e in kwargs["environment"])

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


class _SelfContainer:
    """Minimaler eigener Dashboard-Container (nur attrs wird gelesen)."""

    def __init__(self, networks):
        self.attrs = {"State": {"Running": True},
                      "NetworkSettings": {"Networks": networks}}


class TestNetzwerkAutoHeilung:
    """Bridge-only Dashboard (ZimaOS-App-Import ohne Compose-Netzwerk):
    Docker-DNS löst dort keine Container-Namen auf (RCON: Errno -2) — das
    Dashboard legt deshalb 'mc-dashboard-net' an und startet Instanzen darin."""

    @staticmethod
    def _registriere_self(fake_docker, networks):
        fake_docker.containers._items[socket.gethostname()] = _SelfContainer(networks)

    def test_bridge_only_erstellt_netzwerk(self, fake_docker):
        self._registriere_self(fake_docker, {"bridge": {}})
        assert runtime._network_of_dashboard(fake_docker) == "mc-dashboard-net"
        net = fake_docker.networks._items["mc-dashboard-net"]
        assert net.connected  # Dashboard wurde mit dem Netzwerk verbunden

    def test_benanntes_netzwerk_wird_genutzt(self, fake_docker):
        self._registriere_self(
            fake_docker, {"minedocker_default": {}, "bridge": {}})
        assert runtime._network_of_dashboard(fake_docker) == "minedocker_default"
        assert not fake_docker.networks._items  # nichts neu angelegt

    def test_ohne_eigenen_container_none(self, fake_docker):
        assert runtime._network_of_dashboard(fake_docker) is None

    def test_start_landet_im_auto_netzwerk(self, fake_docker, instanz):
        self._registriere_self(fake_docker, {"bridge": {}})
        runtime.start_instance(instanz)
        assert fake_docker.containers.run_kwargs["network"] == "mc-dashboard-net"


class TestRamNachtraeglich:
    def test_ram_aenderung_wirkt_beim_start(self, fake_docker, instanz):
        """Nachträglich geändertes RAM-Limit (PATCH memory) greift beim
        nächsten (Neu-)Start: Container-Memory-Limit = Heap + 1 GB Headroom,
        itzg-MEMORY-Env setzt den JVM-Heap."""
        instances.update_settings(instanz["id"], memory="3G")
        meta = instances.get_instance(instanz["id"])
        runtime.start_instance(meta)
        kwargs = fake_docker.containers.run_kwargs
        assert kwargs["mem_limit"] == 4 * 1024 ** 3  # 3G Heap + 1G Headroom
        env = dict(item.split("=", 1) for item in kwargs["environment"])
        assert env["MEMORY"] == "3G"


class TestGpu:
    def test_start_mit_gpu(self, fake_docker, instanz):
        ohne = runtime._env(instanz)
        instances.update_settings(instanz["id"], gpu={"enabled": True, "id": "0"})
        inst = instances.get_instance(instanz["id"])
        runtime.start_instance(inst)
        kwargs = fake_docker.containers.run_kwargs
        assert kwargs["runtime"] == "nvidia"
        # abgeleitetes Image mit OpenCL-Loader statt des Basis-Images
        assert kwargs["image"] == "mc-dashboard-minecraft-opencl:java21"
        env = kwargs["environment"]
        assert "NVIDIA_VISIBLE_DEVICES=0" in env
        assert "NVIDIA_DRIVER_CAPABILITIES=compute,utility" in env
        # Bestehende Einstellungen bleiben erhalten
        assert env[:len(ohne)] == ohne
        assert kwargs["ports"] == {"25565/tcp": inst["port"],
                                   "25575/tcp": ("127.0.0.1", inst["rcon_port"])}
        assert kwargs["volumes"][next(iter(kwargs["volumes"]))] == {"bind": "/data", "mode": "rw"}
        assert kwargs["security_opt"] == ["no-new-privileges:true"]
        assert "privileged" not in kwargs

    def test_gpu_id_wird_uebernommen(self, fake_docker, instanz):
        instances.update_settings(instanz["id"], gpu={"enabled": True, "id": "1"})
        runtime.start_instance(instances.get_instance(instanz["id"]))
        assert "NVIDIA_VISIBLE_DEVICES=1" in fake_docker.containers.run_kwargs["environment"]

    def test_deaktiviert_mit_gespeicherter_id_setzt_nichts(self, fake_docker, instanz):
        instances.update_settings(instanz["id"], gpu={"enabled": False, "id": "1"})
        runtime.start_instance(instances.get_instance(instanz["id"]))
        kwargs = fake_docker.containers.run_kwargs
        assert "runtime" not in kwargs
        assert not any(e.startswith("NVIDIA_") for e in kwargs["environment"])

    def test_fehlende_runtime_klare_meldung(self, fake_docker, instanz):
        fake_docker.info = lambda: {"Runtimes": {"runc": {}}}
        instances.update_settings(instanz["id"], gpu={"enabled": True, "id": "0"})
        with pytest.raises(RuntimeError, match="Runtime „nvidia“"):
            runtime.start_instance(instances.get_instance(instanz["id"]))
        assert fake_docker.containers.run_kwargs is None  # kein Start ohne GPU

    def test_runtime_vorhanden_startet(self, fake_docker, instanz):
        fake_docker.info = lambda: {"Runtimes": {"runc": {}, "nvidia": {}}}
        instances.update_settings(instanz["id"], gpu={"enabled": True, "id": "0"})
        runtime.start_instance(instances.get_instance(instanz["id"]))
        assert fake_docker.containers.run_kwargs["runtime"] == "nvidia"

    def test_gpu_start_fehler_wird_uebersetzt(self, fake_docker, instanz):
        def boom(**kwargs):
            raise RuntimeError("Unknown runtime specified nvidia")
        fake_docker.containers.run = boom
        instances.update_settings(instanz["id"], gpu={"enabled": True, "id": "0"})
        with pytest.raises(RuntimeError, match=r"nvidia.*fehlt"):
            runtime.start_instance(instances.get_instance(instanz["id"]))

    def test_gpu_start_anderer_fehler_kein_stiller_fallback(self, fake_docker, instanz):
        calls = []

        def boom(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("nvidia-container-cli: device error")
        fake_docker.containers.run = boom
        instances.update_settings(instanz["id"], gpu={"enabled": True, "id": "0"})
        with pytest.raises(RuntimeError, match="nicht ohne GPU gestartet"):
            runtime.start_instance(instances.get_instance(instanz["id"]))
        assert len(calls) == 1  # kein zweiter Versuch ohne GPU


class TestGpuImage:
    def test_image_wird_einmal_gebaut_und_wiederverwendet(self, fake_docker, instanz):
        instances.update_settings(instanz["id"], gpu={"enabled": True, "id": "0"})
        inst = instances.get_instance(instanz["id"])
        runtime.start_instance(inst)
        runtime.stop_instance(inst)
        runtime.start_instance(inst)  # Neuerstellung des Containers
        assert len(fake_docker.images.builds) == 1
        build = fake_docker.images.builds[0]
        assert build["tag"] == "mc-dashboard-minecraft-opencl:java21"
        assert build["pull"] is True

    def test_dockerfile_installiert_loader(self):
        text = runtime._GPU_DOCKERFILE.format(base="itzg/minecraft-server:java25", rev="1")
        assert text.startswith("FROM itzg/minecraft-server:java25\n")
        assert "ocl-icd-libopencl1" in text and "clinfo" in text
        assert "libOpenCL.so" in text and "ldconfig" in text

    def test_veraltetes_image_wird_neu_gebaut(self, fake_docker, instanz):
        fake_docker.images._items["mc-dashboard-minecraft-opencl:java21"] = \
            runtime_fake_image({"mc-dashboard.gpu-image-rev": "0"})
        instances.update_settings(instanz["id"], gpu={"enabled": True, "id": "0"})
        runtime.start_instance(instances.get_instance(instanz["id"]))
        assert len(fake_docker.images.builds) == 1

    def test_ohne_gpu_kein_build_und_basis_image(self, fake_docker, instanz):
        runtime.start_instance(instanz)
        assert fake_docker.images.builds == []
        assert fake_docker.containers.run_kwargs["image"] == "itzg/minecraft-server:java21"

    def test_build_fehler_kein_start(self, fake_docker, instanz):
        fake_docker.images.build_error = RuntimeError("network down")
        instances.update_settings(instanz["id"], gpu={"enabled": True, "id": "0"})
        with pytest.raises(RuntimeError, match=r"OpenCL-Image.*nicht gebaut"):
            runtime.start_instance(instances.get_instance(instanz["id"]))
        assert fake_docker.containers.run_kwargs is None


def runtime_fake_image(labels):
    from conftest import FakeImage
    return FakeImage(labels)


class TestGpuSettings:
    def test_standard_ist_aus(self, instanz):
        assert instances.gpu_settings(instanz) == {"enabled": False, "id": "0"}
        assert "gpu" not in instanz

    def test_bestandsinstanz_ohne_feld(self):
        assert instances.gpu_settings({"id": "abc"}) == {"enabled": False, "id": "0"}
        assert instances.gpu_settings({"gpu": "kaputt"}) == {"enabled": False, "id": "0"}

    def test_speichern_dauerhaft(self, instanz):
        res = instances.update_settings(instanz["id"], gpu={"enabled": True, "id": "1"})
        assert res["changed"] == ["gpu"]
        assert instances.get_instance(instanz["id"])["gpu"] == {"enabled": True, "id": "1"}

    def test_leere_id_wird_standard(self, instanz):
        instances.update_settings(instanz["id"], gpu={"enabled": True, "id": ""})
        assert instances.get_instance(instanz["id"])["gpu"]["id"] == "0"

    @pytest.mark.parametrize("bad", ["a;b", "0 FOO=1", "-1", "../x", "0,", "1\nX=1"])
    def test_ungueltige_id_abgelehnt(self, instanz, bad):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            instances.update_settings(instanz["id"], gpu={"enabled": True, "id": bad})
        assert exc.value.status_code == 400

    @pytest.mark.parametrize("ok", ["0", "0,1", "GPU-1a2b3c4d-1111-2222-3333-444455556666"])
    def test_gueltige_ids(self, instanz, ok):
        instances.update_settings(instanz["id"], gpu={"enabled": True, "id": ok})
        assert instances.get_instance(instanz["id"])["gpu"]["id"] == ok

    def test_klon_uebernimmt_gpu(self, instanz):
        instances.update_settings(instanz["id"], gpu={"enabled": True, "id": "0"})
        clone = instances.clone_instance(instanz["id"], "Run-Klon")
        assert instances.gpu_settings(clone) == {"enabled": True, "id": "0"}

    def test_api_patch_und_detail(self, client, instanz):
        r = client.patch(f"/api/instances/{instanz['id']}",
                         json={"gpu": {"enabled": True, "id": "0"}})
        assert r.status_code == 200
        assert r.json()["gpu"] == {"enabled": True, "id": "0"}
        assert instances.get_instance(instanz["id"])["gpu"]["enabled"] is True
        bad = client.patch(f"/api/instances/{instanz['id']}",
                           json={"gpu": {"enabled": True, "id": "a;b"}})
        assert bad.status_code == 400
