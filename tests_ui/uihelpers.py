"""Hilfen für die Oberflächen-Tests (eigener Modulname, kollidiert nicht
mit tests/conftest.py)."""
import json

from playwright.sync_api import Page

PING = {"online": True, "version": "Fabric 1.21.4", "motd": "Willkommen!",
        "latency_ms": 3, "error": None}


def plain_headers(headers: dict) -> dict:
    """Antwort-Header für einen umgeschriebenen JSON-Body: ohne GZip-Angaben
    (route.fetch liefert den Body schon entpackt)."""
    out = {k: v for k, v in headers.items()
           if k.lower() not in ("content-encoding", "content-length")}
    out["content-type"] = "application/json"
    return out


def simulate_running(page: Page, state: dict) -> None:
    """Lässt Instanzen im Browser als laufend erscheinen.

    state: {instance_id: {"running": bool, "players": [namen]}} — der Test
    darf das Dict später ändern (z. B. Absturz simulieren)."""

    def live(route):
        live = []
        for inst_id, sim in state.items():
            if sim.get("running"):
                names = sim.get("players", [])
                live.append({"id": inst_id, "name": sim.get("name", inst_id), "port": 25570,
                             "ping": {**PING, "players": {
                                 "online": len(names), "max": 20,
                                 "sample": [{"name": n, "id": n} for n in names]}}})
        route.fulfill(json={"live": live})

    def instances(route):
        resp = route.fetch()
        data = resp.json()

        def fix(inst):
            sim = state.get(inst.get("id"))
            if sim is not None:
                running = bool(sim.get("running"))
                inst["status"] = "running" if running else sim.get("status", "error")
                inst["container"] = {"running": running,
                                     "state": "running" if running else "exited"}
            return inst

        if "instances" in data:
            data["instances"] = [fix(i) for i in data["instances"]]
        else:
            fix(data.get("instance", data))
        route.fulfill(response=resp, body=json.dumps(data),
                      headers=plain_headers(resp.headers))

    page.route("**/api/instances/live", live)
    page.route("**/api/instances", instances)
