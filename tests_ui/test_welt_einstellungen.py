"""Welt-Einstellungen: Spielregeln (RCON, sofort) und Allgemein (server.properties)."""
import json

from playwright.sync_api import expect
from uihelpers import simulate_running


def _open_welt(page, inst):
    page.goto("/")
    page.locator(f'.ov-card[data-id="{inst["id"]}"] .ov-card-title').click()
    expect(page.locator("#detail-title")).to_have_text(inst["name"])
    page.locator('#ws-tabs .slot[data-ws-tab="welt"]').click()
    expect(page.locator(".wset-box")).to_be_visible()


def test_allgemein_speichert_server_properties(page, make_instance, api):
    inst = make_instance("Welt-SMP")
    _open_welt(page, inst)
    # Gestoppt: Spielregeln brauchen einen laufenden Server
    expect(page.locator("#wset-rules-off")).to_be_visible()
    general = page.locator("#wset-general")
    expect(general).to_contain_text("Schwierigkeit")
    expect(general).not_to_contain_text("Sichtweite")  # nur unter „Erweitert“
    save = page.locator("#wset-save")
    expect(save).to_be_disabled()
    general.locator('select[aria-label="Schwierigkeit"]').select_option("hard")
    page.locator("#wset-advanced").check()
    general.locator('input[aria-label="Sichtweite"]').fill("12")
    expect(save).to_be_enabled()
    save.click()
    expect(page.locator("#toasts")).to_contain_text("Welt-Einstellungen gespeichert")
    props = {p["key"]: p["value"] for p in
             api.get(f"/api/instances/{inst['id']}/config").json()["properties"]}
    assert props["difficulty"] == "hard"
    assert props["view-distance"] == "12"
    # Andere Einträge bleiben erhalten
    assert props["motd"].startswith("Welt-SMP")
    assert not page.js_errors


def test_spielregeln_und_uhrzeit_bei_laufendem_server(page, make_instance):
    inst = make_instance("Welt-Live")
    simulate_running(page, {inst["id"]: {"running": True, "name": inst["name"]}})
    rules = [
        {"name": "playersSleepingPercentage", "type": "int", "value": 100,
         "default": 100, "min": 0, "max": 100, "desc": "", "raw": "100"},
        {"name": "doDaylightCycle", "type": "bool", "value": True,
         "default": True, "min": None, "max": None, "desc": "", "raw": "true"},
        {"name": "keepInventory", "type": "bool", "value": False,
         "default": False, "min": None, "max": None, "desc": "", "raw": "false"},
    ]
    sent = []

    def gamerules(route):
        if route.request.method == "POST":
            body = json.loads(route.request.post_data or "{}")
            sent.append(body)
            route.fulfill(json={"name": body["name"], "value": body["value"], "output": ""})
        else:
            route.fulfill(json={"gamerules": rules, "daytime": 6000})

    def time_weather(route):
        sent.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(json={"commands": [], "daytime": 18000})

    page.route(f"**/api/instances/{inst['id']}/gamerules", gamerules)
    page.route(f"**/api/instances/{inst['id']}/world/time-weather", time_weather)
    _open_welt(page, inst)
    rows = page.locator("#wset-rules .wset-row")
    # Locator-Bar meldet der Server (1.21.4) nicht → keine Zeile dafür
    expect(rows).to_have_count(3)
    expect(page.locator("#wset-rules")).not_to_contain_text("Ortungsleiste")
    expect(page.locator("#wset-time")).to_have_text("12:00")
    page.locator("#wset-rules .wset-row", has_text="Inventar behalten") \
        .locator(".wset-switch input").check()
    expect(page.locator("#toasts")).to_contain_text("Inventar behalten: an")
    slider = page.locator('#wset-rules input[type="range"]')
    slider.fill("0")
    expect(page.locator(".wset-slider-val")).to_have_text("0 %")
    page.locator('[data-wset-time="nacht"]').click()
    expect(page.locator("#wset-time")).to_have_text("00:00")
    assert {"name": "keepInventory", "value": True} in sent
    assert {"name": "playersSleepingPercentage", "value": 0} in sent
    assert {"time": "nacht"} in sent
    assert not page.js_errors
