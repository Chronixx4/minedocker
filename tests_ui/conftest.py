"""Oberflächen-Tests: echtes Dashboard (uvicorn im Unterprozess) + Chromium.

Docker gibt es hier nicht — wo ein laufender Server nötig ist, schreibt
`simulate_running` die API-Antworten im Browser um (page.route). Alles
andere (Instanzen, Welten, Einstellungen) läuft gegen das echte Backend
mit eigenem Datenverzeichnis.

Lokal ohne `playwright install`: PW_CHROMIUM=/pfad/zu/chrome setzen.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
# Screenshots fehlgeschlagener Tests + Server-Log (CI lädt den Ordner hoch)
ARTIFACTS = ROOT / "ui-artifacts"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    setattr(item, f"rep_{report.when}", report)
    return report


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def data_dir():
    return Path(tempfile.mkdtemp(prefix="mc-dash-ui-"))


@pytest.fixture(scope="session")
def base_url(data_dir):
    port = _free_port()
    env = {
        **os.environ,
        "INSTANCES_DIR": str(data_dir / "instances"),
        "INSTANCES_HOST_DIR": str(data_dir / "instances"),
        "INSTANCES_PORT_BASE": "25570",
        "MODS_DIR": str(data_dir / "mods"),
        "HISTORY_DB": str(data_dir / "history.db"),
        "MC_HOST": "127.0.0.1",
        "MC_PORT": "59999",
        "DASHBOARD_API_KEY": "",
        "DOCKER_HOST": "unix:///nonexistent.sock",  # nie einen echten Daemon anfassen
        "ITEM_ICONS_VANILLA": "false",  # keine Mojang-Downloads
    }
    log = open(data_dir / "uvicorn.log", "wb")  # noqa: SIM115 — lebt bis Sessionende
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while True:
        try:
            if httpx.get(f"{url}/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            raise RuntimeError("Dashboard startet nicht:\n"
                               + (data_dir / "uvicorn.log").read_text(errors="replace"))
        time.sleep(0.2)
    yield url
    proc.terminate()
    proc.wait(timeout=10)
    log.close()
    ARTIFACTS.mkdir(exist_ok=True)
    (ARTIFACTS / "uvicorn.log").write_bytes((data_dir / "uvicorn.log").read_bytes())


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as pw:
        exe = os.environ.get("PW_CHROMIUM") or None
        browser = pw.chromium.launch(executable_path=exe)
        yield browser
        browser.close()


@pytest.fixture()
def api(base_url):
    with httpx.Client(base_url=base_url, timeout=10) as client:
        yield client


@pytest.fixture(autouse=True)
def _leere_instanzen(data_dir, base_url, api):
    """Jeder Test startet ohne Instanzen (über die API, damit Caches stimmen)."""
    for inst in api.get("/api/instances").json()["instances"]:
        api.delete(f"/api/instances/{inst['id']}", params={"force": True})
    yield


@pytest.fixture()
def make_instance(api):
    def _make(name: str, loader: str = "fabric", game_version: str = "1.21.4") -> dict:
        resp = api.post("/api/instances", json={"name": name, "loader": loader,
                                                "game_version": game_version,
                                                "accept_eula": True})
        assert resp.status_code == 201, resp.text
        return resp.json()
    return _make


def _new_page(browser: Browser, base_url: str, request, **context_args):
    context = browser.new_context(base_url=base_url, **context_args)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on("console", lambda msg: msg.type == "error"
            and "Failed to load resource" not in msg.text  # absichtliche 4xx/5xx
            and errors.append(f"console: {msg.text}"))
    page.js_errors = errors  # type: ignore[attr-defined]
    return context, page


def _finish(page, context, request) -> None:
    """Screenshot bei Fehlschlag; danach JS-Fehler prüfen."""
    report = getattr(request.node, "rep_call", None)
    if report is not None and report.failed:
        ARTIFACTS.mkdir(exist_ok=True)
        try:
            page.screenshot(path=str(ARTIFACTS / f"{request.node.name}.png"), full_page=True)
        except Exception:
            pass  # Screenshot ist nur eine Hilfe
    errors = page.js_errors  # type: ignore[attr-defined]
    context.close()
    assert errors == [], "JavaScript-Fehler:\n" + "\n".join(errors)


@pytest.fixture()
def page(browser, base_url, request):
    """Desktop-Seite; jeder JS-Fehler lässt den Test scheitern."""
    context, page = _new_page(browser, base_url, request,
                              viewport={"width": 1440, "height": 900})
    yield page
    _finish(page, context, request)


@pytest.fixture()
def phone(browser, base_url, request):
    context, page = _new_page(browser, base_url, request,
                              viewport={"width": 390, "height": 844},
                              is_mobile=True, has_touch=True)
    yield page
    _finish(page, context, request)
