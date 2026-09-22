"""Test-Konfiguration: Umgebungsvariablen setzen, BEVOR app importiert wird."""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="mc-dash-test-")
os.environ["MODS_DIR"] = os.path.join(_TMP, "mods")
os.environ["MC_HOST"] = "127.0.0.1"
os.environ["MC_PORT"] = "59999"  # geschlossen -> Offline-Pfad
os.environ["MC_VERSION"] = "1.21.4"
os.environ["MOD_LOADER"] = "fabric"
os.environ["DASHBOARD_API_KEY"] = ""
os.environ["CORS_ORIGINS"] = "*"
# Multi-Server: eigene Instanz-Umgebung für Tests
os.environ["INSTANCES_DIR"] = os.path.join(_TMP, "instances")
os.environ["INSTANCES_HOST_DIR"] = os.environ["INSTANCES_DIR"]
os.environ["INSTANCES_PORT_BASE"] = "25570"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _isoliere_auth():
    """users.json/.auth_secret vor und nach JEDEM Test löschen — die Dateien
    liegen im geteilten conftest-tmp; ohne Reset würde ein Login-Test, der
    Benutzer anlegt, alle Folge-Tests (401) sperren. In-Memory-Sperrliste
    und Secret-Cache werden ebenfalls zurückgesetzt."""
    from app import auth as auth_mod

    auth_mod.reset_for_tests()
    for path in (auth_mod.users_path(), auth_mod._secret_path()):
        path.unlink(missing_ok=True)
    yield
    auth_mod.reset_for_tests()
    for path in (auth_mod.users_path(), auth_mod._secret_path()):
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Fake-Docker-Client für runtime-Tests (kein Docker-Daemon nötig)
# ---------------------------------------------------------------------------

class NotFoundError(Exception):
    pass


class FakeContainer:
    def __init__(self, name, running=False):
        self.name = name
        self.attrs = {"State": {"Running": running,
                                "Status": "running" if running else "exited",
                                "StartedAt": "2026-01-01T12:00:00.123456789Z"
                                if running else None}}
        self.removed = False
        self._logs = b""

    def stop(self, timeout=None):
        self.attrs["State"]["Running"] = False

    def remove(self, force=False):
        self.removed = True

    def logs(self, tail=None, timestamps=False):
        lines = self._logs.splitlines()
        if tail:
            lines = lines[-int(tail):]
        return b"\n".join(lines)


class FakeContainers:
    def __init__(self):
        self._items = {}
        self.run_kwargs = None

    def get(self, name):
        if name not in self._items:
            raise NotFoundError(f"Container {name} not found")
        return self._items[name]

    def run(self, **kwargs):
        name = kwargs["name"]
        container = FakeContainer(name, running=True)
        self._items[name] = container
        self.run_kwargs = kwargs
        return container


class FakeNetwork:
    def __init__(self, name):
        self.name = name
        self.connected = []

    def connect(self, container):
        self.connected.append(container)


class FakeNetworks:
    def __init__(self):
        self._items = {}

    def get(self, name):
        if name not in self._items:
            raise NotFoundError(f"Network {name} not found")
        return self._items[name]

    def create(self, name, driver=None):
        net = FakeNetwork(name)
        self._items[name] = net
        return net


class FakeDockerClient:
    def __init__(self):
        self.containers = FakeContainers()
        self.networks = FakeNetworks()


@pytest.fixture()
def fake_docker(monkeypatch):
    """Injiziert einen Fake-Docker-Client in app.runtime."""
    from app import runtime

    client = FakeDockerClient()
    monkeypatch.setattr(runtime, "_client", client)
    return client
