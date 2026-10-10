"""Schlafmodus, Crash-Diagnose, Versionswechsel und Bereiche, die erst beim
Öffnen laden."""
import json

from playwright.sync_api import expect
from uihelpers import plain_headers


def _open(page, inst, slot=None):
    page.goto("/")
    page.locator(f'.ov-card[data-id="{inst["id"]}"] .ov-card-title').click()
    expect(page.locator("#detail-title")).to_have_text(inst["name"])
    if slot:
        page.locator(f'#ws-tabs .slot[data-ws-tab="{slot}"]').click()


def test_schlafmodus_speichern(page, make_instance, api):
    inst = make_instance("Schlaf-SMP")
    _open(page, inst, "einstellungen")
    expect(page.locator("#hib-mode")).to_be_visible()
    page.select_option("#hib-mode", "pause")
    page.fill("#hib-minutes", "15")
    page.click("#hib-save")
    expect(page.locator("#toasts")).to_contain_text("Schlafmodus gespeichert")
    data = api.get(f"/api/instances/{inst['id']}").json()
    assert data["hibernate"] == {"mode": "pause", "minutes": 15}


def test_schlafender_server_heisst_schlaeft(page, make_instance):
    inst = make_instance("Schlaf-SMP")

    def schlafend(route):
        resp = route.fetch()
        data = resp.json()
        for item in data.get("instances", [data]):
            item["status"] = "running"
            item["container"] = {"running": True, "state": "running", "paused": True}
        route.fulfill(response=resp, body=json.dumps(data),
                      headers=plain_headers(resp.headers))

    page.route("**/api/instances", schlafend)
    page.route(f"**/api/instances/{inst['id']}", schlafend)
    _open(page, inst)
    expect(page.locator("#detail-state-text")).to_have_text("schläft")


def test_crash_diagnose_anzeigen_und_ausblenden(page, make_instance, api, data_dir):
    inst = make_instance("Crash-SMP")
    reports = data_dir / "instances" / inst["id"] / "crash-reports"
    reports.mkdir(parents=True)
    (reports / "crash-2026.txt").write_text(
        "java.lang.OutOfMemoryError: Java heap space", encoding="utf-8")
    api.post(f"/api/instances/{inst['id']}/crash/analyze")
    _open(page, inst)
    expect(page.locator("#crash-box")).to_be_visible()
    expect(page.locator("#crash-title")).to_contain_text("Zu wenig Arbeitsspeicher")
    page.click("#crash-dismiss")
    expect(page.locator("#crash-box")).to_be_hidden()


def test_bereiche_laden_erst_beim_oeffnen(page, make_instance):
    inst = make_instance("Lazy-SMP")
    requests = []
    page.on("request", lambda r: requests.append(r.url))
    _open(page, inst)
    page.wait_for_timeout(500)
    assert not any("/backups" in u for u in requests)
    page.locator('#ws-tabs .slot[data-ws-tab="backups"]').click()
    expect(page.locator("#backup-location")).to_contain_text("Ablage")
    assert any(f"/instances/{inst['id']}/backups" in u for u in requests)


def test_versionswechsel_vorschau(page, make_instance):
    inst = make_instance("Wechsel-SMP")
    page.route("**/api/catalog/mc-versions", lambda route: route.fulfill(json={
        "releases": ["1.21.5", "1.21.4", "1.20.1"], "snapshots": [],
        "latest_release": "1.21.5"}))
    page.route("**/api/catalog/loaders?*", lambda route: route.fulfill(json={
        "game_version": "x", "loaders": [
            {"loader": "fabric", "versions": [{"version": "0.16.9", "stable": True}],
             "note": None, "error": None}]}))
    page.route(f"**/api/instances/{inst['id']}/version/check", lambda route: route.fulfill(json={
        "current": {"loader": "fabric", "game_version": "1.21.4"},
        "target": {"loader": "fabric", "game_version": "1.20.1"},
        "same": False, "downgrade": True, "loader_changed": False, "modpack": None,
        "mods": {"available": [{"filename": "a.jar", "from": "1", "to": "2", "unchanged": False}],
                 "missing": [{"filename": "b.jar"}], "unknown": []}}))
    _open(page, inst, "einstellungen")
    expect(page.locator("#ver-mc-version")).to_have_value("1.21.4")
    page.select_option("#ver-mc-version", "1.20.1")
    expect(page.locator("#ver-loader")).to_have_value("fabric")
    page.click("#ver-check")
    expect(page.locator("#ver-summary")).to_contain_text("1 Mods werden aktualisiert")
    expect(page.locator("#ver-mods li")).to_have_count(2)
    expect(page.locator("#ver-downgrade-wrap")).to_be_visible()
