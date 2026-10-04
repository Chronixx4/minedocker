"""Server-Ansicht: Hotbar, Befehlspalette, Aktivität & Chat, Welten."""
import json
import re

from playwright.sync_api import expect
from uihelpers import simulate_running

SLOTS = ["uebersicht", "konsole", "spieler", "mods", "welt",
         "dateien", "backups", "zeitplan", "einstellungen"]


def _open(page, inst):
    page.goto("/")
    page.locator(f'.ov-card[data-id="{inst["id"]}"] .ov-card-title').click()
    expect(page.locator("#detail-title")).to_have_text(inst["name"])


def test_hotbar_tasten_1_bis_9(page, make_instance):
    _open(page, make_instance("Survival-SMP"))
    for key, slot in enumerate(SLOTS, start=1):
        page.keyboard.press(str(key))
        expect(page.locator("#ws-tabs .slot.active")).to_have_attribute("data-ws-tab", slot)
        expect(page.locator(f'.ws-panel[data-ws-panel="{slot}"]')).to_be_visible()


def test_befehlspalette_springt_zu_server_und_bereich(page, make_instance):
    make_instance("Survival-SMP")
    kreativ = make_instance("Kreativ")
    page.goto("/")
    expect(page.locator("#ov-grid .ov-card")).to_have_count(2)
    page.keyboard.press("Control+k")
    expect(page.locator("#cmdk-input")).to_be_focused()
    page.keyboard.type("kreativ welt")
    expect(page.locator("#cmdk-list .cmdk-item.active")).to_contain_text("Welt")
    page.keyboard.press("Enter")
    expect(page.locator("#cmdk")).not_to_have_attribute("open", "")
    expect(page.locator("#detail-title")).to_have_text(kreativ["name"])
    expect(page.locator("#ws-tabs .slot.active")).to_have_attribute("data-ws-tab", "welt")
    page.keyboard.press("Control+k")
    page.keyboard.press("Escape")
    expect(page.locator("#cmdk")).not_to_have_attribute("open", "")


def test_befehlspalette_sendet_befehl(page, make_instance):
    inst = make_instance("Survival-SMP")
    simulate_running(page, {inst["id"]: {"running": True, "name": inst["name"]}})
    sent = []

    def console(route):
        sent.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(json={"output": "[Server] hallo"})

    page.route(f"**/api/instances/{inst['id']}/console", console)
    page.goto("/")
    expect(page.locator("#ov-summary")).to_contain_text("1 von 1 Server läuft")
    page.keyboard.press("Control+k")
    page.keyboard.type("/say hallo")
    expect(page.locator("#cmdk-list .cmdk-item.active")).to_contain_text("an Survival-SMP")
    page.keyboard.press("Enter")
    expect(page.locator("#toasts")).to_contain_text("hallo")
    assert sent == [{"command": "say hallo"}]


def test_aktivitaet_aus_dem_log(page, make_instance):
    inst = make_instance("Survival-SMP")
    lines = [
        "[12:00:01] [Server thread/INFO]: Done (4.2s)! For help, type \"help\"",
        "[12:01:00] [Server thread/INFO]: Steve joined the game",
        "[12:01:30] [Server thread/INFO]: <Steve> hallo zusammen",
        "[12:02:00] [Server thread/INFO]: Steve has made the advancement [Stone Age]",
        "[12:03:00] [Server thread/INFO]: Steve was slain by Zombie",
        "[12:04:00] [Server thread/INFO]: Steve left the game",
        "[12:04:30] [Server thread/INFO]: Preparing spawn area: 83%",  # kein Ereignis
    ]
    page.route(f"**/api/instances/{inst['id']}/logs?*",
               lambda route: route.fulfill(json={"logs": lines}))
    _open(page, inst)
    items = page.locator("#ws-activity li")
    expect(items).to_have_count(6)
    texts = items.all_inner_texts()
    assert "ist bereit" in texts[0]
    assert "Steve" in texts[1] and "beigetreten" in texts[1]
    assert "hallo zusammen" in texts[2]
    assert "Stone Age" in texts[3]
    assert "slain by Zombie" in texts[4]
    assert "verlassen" in texts[5]
    # Polling liefert dieselben Zeilen erneut → keine Duplikate
    page.locator("#detail-logs-reload").click()
    page.wait_for_timeout(300)
    expect(items).to_have_count(6)


def test_welten_liste_und_neue_welt(page, make_instance, data_dir):
    inst = make_instance("Survival-SMP")
    base = data_dir / "instances" / inst["id"]
    for name in ("world", "Skyblock"):
        (base / name / "region").mkdir(parents=True, exist_ok=True)
        (base / name / "level.dat").write_bytes(b"LEVEL")
    _open(page, inst)
    page.keyboard.press("5")
    rows = page.locator("#worlds-list .world-row")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text("world")
    page.locator("#world-new-box summary").click()
    page.fill("#world-new-name", "Abenteuer")
    page.fill("#world-new-seed", "4242")
    page.click("#world-new-btn")
    expect(rows).to_have_count(3)
    expect(rows.first).to_contain_text("Abenteuer")
    props = (base / "server.properties").read_text()
    assert re.search(r"^level-name=Abenteuer$", props, re.M)
    assert re.search(r"^level-seed=4242$", props, re.M)



def _detail_ohne_speicher(page, inst):
    """Detail-Antwort so umschreiben, als liefe die Speicher-Zählung noch."""
    def detail(route):
        resp = route.fetch()
        data = resp.json()
        data["disk"] = None
        data["disk_pending"] = True
        route.fulfill(response=resp, json=data)

    page.route(f"**/api/instances/{inst['id']}", detail)


def test_speicher_wird_berechnet(page, make_instance):
    inst = make_instance("Grosse-Welt")
    _detail_ohne_speicher(page, inst)
    page.route(f"**/api/instances/{inst['id']}/disk",
               lambda route: route.fulfill(status=503, json={"detail": "noch nicht"}))
    _open(page, inst)
    expect(page.locator("#detail-info")).to_contain_text("wird berechnet…")


def test_speicher_wird_nachgeladen(page, make_instance):
    inst = make_instance("Grosse-Welt")
    _detail_ohne_speicher(page, inst)
    page.route(f"**/api/instances/{inst['id']}/disk", lambda route: route.fulfill(json={
        "total_bytes": 3 * 1024 ** 3, "mods_bytes": 0, "world_bytes": 3 * 1024 ** 3,
        "packs_bytes": 0, "rest_bytes": 0, "world_dir": "world", "world_exists": True}))
    _open(page, inst)
    expect(page.locator("#detail-info")).to_contain_text("Welt 3")
    expect(page.locator("#detail-info")).not_to_contain_text("wird berechnet")
