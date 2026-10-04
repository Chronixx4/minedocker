"""Spieler-Übersicht (Dateien des Servers) und Live-Tabelle (RCON, simuliert)."""
import gzip
import json
import struct

from playwright.sync_api import expect
from uihelpers import simulate_running

U1 = "11111111-2222-3333-4444-555555555555"
U2 = "66666666-7777-8888-9999-000000000000"


def _nbt(root: dict) -> bytes:
    def enc(value):
        if isinstance(value, int):
            return 3, struct.pack(">i", value)
        if isinstance(value, float):
            return 6, struct.pack(">d", value)
        if isinstance(value, str):
            raw = value.encode()
            return 8, struct.pack(">H", len(raw)) + raw
        if isinstance(value, list):
            parts = [enc(v) for v in value]
            return 9, struct.pack(">bi", parts[0][0], len(parts)) + b"".join(p for _, p in parts)
        body = b""
        for key, child in value.items():
            tag, payload = enc(child)
            body += struct.pack(">bH", tag, len(key)) + key.encode() + payload
        return 10, body + b"\x00"
    return gzip.compress(b"\x0a\x00\x00" + enc(root)[1])


def _welt(data_dir, inst):
    base = data_dir / "instances" / inst["id"]
    world = base / "world"
    for sub in ("stats", "advancements", "playerdata"):
        (world / sub).mkdir(parents=True, exist_ok=True)
    (world / "level.dat").write_bytes(_nbt({"Data": {"raining": 0}}))
    (base / "usercache.json").write_text(json.dumps([
        {"name": "BuildTheWorld", "uuid": U1}, {"name": "Kaz_Umi_", "uuid": U2}]))
    (base / "ops.json").write_text(json.dumps([{"uuid": U2, "name": "Kaz_Umi_", "level": 4}]))
    (world / "stats" / f"{U1}.json").write_text(json.dumps({"stats": {
        "minecraft:custom": {"minecraft:play_time": 20 * 3600 * 130, "minecraft:mob_kills": 12522,
                             "minecraft:player_kills": 26, "minecraft:deaths": 1,
                             "minecraft:walk_one_cm": 32980000},
        "minecraft:mined": {"minecraft:diamond_ore": 302}}}))
    (world / "advancements" / f"{U1}.json").write_text(json.dumps({
        "minecraft:story/mine_stone": {"criteria": {"a": "2026-01-30 10:00:00 +0000"}, "done": True}}))
    (world / "playerdata" / f"{U1}.dat").write_bytes(_nbt({
        "Health": 20.0, "foodLevel": 20, "XpLevel": 27, "Pos": [389.5, 106.0, -225.2],
        "Dimension": "minecraft:overworld", "playerGameType": 0, "Rotation": [90.0, 0.0]}))


def _open_spieler(page, inst):
    page.goto("/")
    page.locator(f'.ov-card[data-id="{inst["id"]}"] .ov-card-title').click()
    expect(page.locator("#detail-title")).to_have_text(inst["name"])
    page.locator('#ws-tabs .slot[data-ws-tab="spieler"]').click()
    expect(page.locator(".pl-box")).to_be_visible()


def test_uebersicht_aus_dateien(page, make_instance, data_dir):
    inst = make_instance("Spieler-SMP")
    _welt(data_dir, inst)
    _open_spieler(page, inst)
    items = page.locator("#pl-list .pl-item")
    expect(items).to_have_count(2)
    expect(page.locator('[data-pl-filter="op"]')).to_have_text("Operator (1)")
    page.locator('[data-pl-filter="op"]').click()
    expect(items).to_have_count(1)
    expect(items.first).to_contain_text("Kaz_Umi_")
    page.locator('[data-pl-filter="all"]').click()
    page.locator("#pl-list .pl-item", has_text="BuildTheWorld").click()
    profile = page.locator("#pl-profile")
    expect(profile.locator(".pl-head-name h4")).to_have_text("BuildTheWorld")
    expect(profile).to_contain_text("12.522")
    expect(profile).to_contain_text("302")
    expect(profile).to_contain_text("329,8 km")
    expect(profile).to_contain_text("390, 106, -225")
    profile.locator('[data-pl-tab="fortschritte"]').click()
    expect(profile.locator(".pl-adv-item")).to_have_count(1)
    profile.locator('[data-pl-tab="statistiken"]').click()
    expect(profile).to_contain_text("Gehen")
    page.locator("#pl-filter").fill("kaz")
    expect(items).to_have_count(1)
    # Gestoppt: keine Live-Tabelle
    expect(page.locator("#plive-hint")).to_contain_text("Server läuft nicht")
    assert not page.js_errors


def test_live_tabelle(page, make_instance, data_dir):
    inst = make_instance("Live-SMP")
    _welt(data_dir, inst)
    simulate_running(page, {inst["id"]: {"running": True, "name": inst["name"],
                                         "players": ["BuildTheWorld"]}})
    page.route(f"**/api/instances/{inst['id']}/players-live", lambda route: route.fulfill(json={
        "players": [{"name": "BuildTheWorld", "health": 8.5, "food": 17, "level": 27,
                     "pos": [389, 106, -225], "dimension": "minecraft:overworld",
                     "bed": {"pos": [3378, 18, -1008], "dimension": "minecraft:overworld"},
                     "session_seconds": 506}],
        "online": 1, "max": 20, "daytime": 16333, "weather": "klar", "fetched_at": 0}))
    _open_spieler(page, inst)
    rows = page.locator("#plive-body tr")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("8 min")
    expect(rows.first).to_contain_text("3378, 18, -1008")
    expect(rows.first.locator(".plive-num.warn")).to_have_text("8.5")
    expect(page.locator("#plive-time")).to_contain_text("22:19")
    expect(page.locator("#plive-count")).to_have_text("1 / 20")
    rows.first.locator(".plive-name").click()
    expect(page.locator("#pl-profile .pl-head-name h4")).to_have_text("BuildTheWorld")
    assert not page.js_errors
