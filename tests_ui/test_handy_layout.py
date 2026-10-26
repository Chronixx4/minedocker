"""Handy-Layout: Ordnernamen im Dateimanager vollständig lesbar."""
from playwright.sync_api import expect


def test_dateimanager_namen_nicht_abgeschnitten(phone, make_instance, data_dir):
    inst = make_instance("Handy-SMP")
    long_dir = data_dir / "instances" / inst["id"] / "journeymap-backup-ordner"
    long_dir.mkdir(parents=True)
    phone.goto("/")
    phone.locator(f'.ov-card[data-id="{inst["id"]}"] .ov-card-title').click()
    expect(phone.locator("#detail-title")).to_have_text(inst["name"])
    phone.locator('#ws-tabs .slot[data-ws-tab="dateien"]').click()
    name = phone.locator("#fb-list .mod-name", has_text="journeymap-backup-ordner")
    expect(name).to_be_visible()
    cut = name.evaluate("e => e.scrollWidth > e.clientWidth + 1")
    assert not cut, "Ordnername ist abgeschnitten"
    assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
