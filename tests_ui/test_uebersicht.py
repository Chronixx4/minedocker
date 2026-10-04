"""Startseite „Alle Server“, Server-Leiste, Tab-Titel, Handy-Layout."""
import re

from playwright.sync_api import expect
from uihelpers import simulate_running


def test_leere_startseite(page):
    page.goto("/")
    expect(page.locator("#ov-summary")).to_have_text("Noch keine Welt angelegt")
    expect(page.locator("#ov-grid .ov-card-add")).to_be_visible()


def test_karten_und_zusammenfassung(page, make_instance):
    smp = make_instance("Survival-SMP")
    make_instance("Kreativ", loader="paper")
    simulate_running(page, {smp["id"]: {"running": True, "name": "Survival-SMP",
                                        "players": ["Steve", "Alex"]}})
    page.goto("/")
    cards = page.locator("#ov-grid .ov-card")
    expect(cards).to_have_count(2)
    expect(page.locator(f'.ov-card[data-id="{smp["id"]}"]')).to_contain_text("Survival-SMP")
    expect(page.locator("#ov-summary")).to_contain_text("1 von 2 Servern laufen")
    expect(page.locator("#ov-summary")).to_contain_text("2 Spieler online")
    expect(page).to_have_title("● 2 online · Minedocker")
    # Spielerzahl auch im Chip der Server-Leiste
    expect(page.locator(f'#inst-tabs [data-id="{smp["id"]}"] .inst-tab-count')).to_have_text("2")


def test_karte_oeffnet_server(page, make_instance):
    inst = make_instance("Survival-SMP")
    page.goto("/")
    page.locator(f'.ov-card[data-id="{inst["id"]}"] .ov-card-title').click()
    expect(page.locator("#ws-tabs .slot.active")).to_have_attribute("data-ws-tab", "uebersicht")
    expect(page.locator(f'#inst-tabs [data-id="{inst["id"]}"]')).to_have_class(
        re.compile(r"\bactive\b"))
    expect(page.locator("#detail-title")).to_have_text("Survival-SMP")


def test_handy_ohne_seitliches_scrollen(phone, make_instance):
    inst = make_instance("Ein ziemlich langer Servername fuer das Handy")
    phone.goto("/")
    expect(phone.locator("#ov-grid .ov-card")).to_have_count(1)
    overflow = "document.documentElement.scrollWidth - window.innerWidth"
    assert phone.evaluate(overflow) <= 0
    phone.locator(f'.ov-card[data-id="{inst["id"]}"] .ov-card-title').click()
    expect(phone.locator("#ws-tabs .slot.active")).to_be_visible()
    for key in ("2", "5", "9"):
        phone.keyboard.press(key)
        assert phone.evaluate(overflow) <= 0, f"seitlich scrollbar im Slot {key}"
