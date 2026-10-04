"""Spieler-Inventar: Fenster öffnen, Slots ansehen, Item verschieben, geben."""
import json

from playwright.sync_api import expect
from uihelpers import simulate_running


def _item(slot, item_id, count=1, **extra):
    return {"slot": slot, "id": item_id, "count": count, "damage": None, "max_damage": None,
            "custom_name": None, "enchantments": [], "data": None, "sig": f"sig-{slot}",
            "movable": True, **extra}


def snapshot(slots):
    return {
        "player": "Steve", "selected_slot": 0, "mc_version": "1.21.1", "loader": "neoforge",
        "editable": True, "fetched_at": 1_700_000_000, "icons": "disabled",
        "stats": {"health": 18.0, "food": 17, "level": 34, "pos": [214, 71, -388],
                  "dimension": "minecraft:overworld", "gamemode": "survival"},
        "slots": {s["slot"]: s for s in slots},
        "mod_items": [{**_item("", "artifacts:gold_ring"), "group": "ring", "movable": False}],
    }


START = [
    _item("container.0", "minecraft:netherite_sword", damage=157, max_damage=2031,
          enchantments=[{"id": "minecraft:sharpness", "level": 5}],
          data='{"minecraft:damage":157}'),
    _item("container.9", "mekanism:ingot_osmium", 48),
    _item("armor.head", "minecraft:netherite_helmet"),
    _item("enderchest.3", "minecraft:diamond", 12),
]


def _setup(page, make_instance):
    inst = make_instance("ATM10", "neoforge", "1.21.1")
    simulate_running(page, {inst["id"]: {"running": True, "name": inst["name"], "players": ["Steve"]}})
    base = f"**/api/instances/{inst['id']}"
    page.route(f"{base}/players", lambda r: r.fulfill(json={
        "online": 1, "max": 20, "names": ["Steve"], "raw": ""}))
    posted = []
    current = {"snap": snapshot(START)}

    def inventory(route):
        if route.request.method == "POST":
            body = json.loads(route.request.post_data or "{}")
            posted.append(body)
            if body["action"] == "move":
                slots = {k: v for k, v in current["snap"]["slots"].items()}
                moved = slots.pop(body["slot"])
                slots[body["to"]] = {**moved, "slot": body["to"], "sig": "neu"}
                current["snap"] = snapshot(list(slots.values()))
        route.fulfill(json=current["snap"])

    page.route(f"{base}/players/Steve/inventory", inventory)
    page.route(f"{base}/items?*", lambda r: r.fulfill(json={
        "items": {"mekanism:ingot_osmium": {"name": "Osmiumbarren"}},
        "results": [{"id": "mekanism:ingot_osmium", "name": "Osmiumbarren"},
                    {"id": "mekanism:block_osmium", "name": "Osmiumblock"}],
        "icons": "disabled"}))
    page.route(f"{base}/items/icon?*", lambda r: r.fulfill(status=404, json={"detail": "x"}))
    page.goto("/")
    page.locator(f'.ov-card[data-id="{inst["id"]}"] .ov-card-title').click()
    page.keyboard.press("3")
    page.locator("#rcon-reload").click()
    page.locator("#rcon-players .player-card", has_text="Steve").get_by_role(
        "button", name="Inventar").click()
    expect(page.locator("#inv-dialog")).to_be_visible()
    return posted


def test_inventar_ansehen_und_verschieben(page, make_instance):
    posted = _setup(page, make_instance)
    expect(page.locator("#inv-title")).to_have_text("Inventar · Steve")
    expect(page.locator("#inv-hotbar .inv-slot")).to_have_count(9)
    expect(page.locator("#inv-main .inv-slot")).to_have_count(27)
    expect(page.locator('#inv-hotbar [data-slot="container.0"]')).to_have_class("inv-slot hand")
    # Mod-Namen kommen aus der Namens-Abfrage
    expect(page.locator('#inv-main [data-slot="container.9"]')).to_have_attribute(
        "title", "Osmiumbarren (mekanism:ingot_osmium)")
    expect(page.locator('#inv-main [data-slot="container.9"] .inv-n')).to_have_text("48")

    page.locator('#inv-hotbar [data-slot="container.0"]').click()
    expect(page.locator("#inv-det-id")).to_contain_text("minecraft:netherite_sword · Hotbar 1")
    expect(page.locator("#inv-det-kv")).to_contain_text("1.874 / 2.031")
    expect(page.locator("#inv-det-kv")).to_contain_text("Sharpness 5")

    page.locator("#inv-det-target").select_option("container.5")
    page.locator("#inv-det-move").click()
    expect(page.locator('#inv-hotbar [data-slot="container.5"] img')).to_have_count(0)
    expect(page.locator('#inv-hotbar [data-slot="container.5"] .inv-fb')).to_be_visible()
    assert posted[-1] == {"action": "move", "slot": "container.0", "to": "container.5",
                          "expect": {"container.0": "sig-container.0", "container.5": ""}}

    page.locator('[data-inv-tab="ender"]').click()
    expect(page.locator('#inv-ender [data-slot="enderchest.3"] .inv-n')).to_have_text("12")
    page.locator('[data-inv-tab="mods"]').click()
    expect(page.locator("#inv-mods")).to_contain_text("Ring")


def test_item_geben(page, make_instance):
    posted = _setup(page, make_instance)
    page.locator("#inv-give-q").fill("osmium")
    expect(page.locator("#inv-give-results li")).to_have_count(2)
    page.locator("#inv-give-results li", has_text="Osmiumblock").click()
    page.locator("#inv-give-count").fill("64")
    page.locator("#inv-give-btn").click()
    expect(page.locator("#toasts")).to_contain_text("64\u00d7 Osmiumblock gegeben")
    assert posted[-1] == {"action": "give", "item_id": "mekanism:block_osmium", "count": 64,
                          "expect": {}}


def test_loeschen_fragt_nach(page, make_instance):
    posted = _setup(page, make_instance)
    page.locator('#inv-main [data-slot="container.9"]').click(button="right")
    expect(page.locator("#confirm-dialog")).to_be_visible()
    expect(page.locator("#cd-message")).to_contain_text("48\u00d7 Osmiumbarren")
    page.locator("#cd-cancel").click()
    assert posted == []
