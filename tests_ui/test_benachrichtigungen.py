"""Benachrichtigungen: Absturz/Bereit/Beitritt — auch bei verstecktem Tab
(Regression v1.16.1: im Hintergrund wurde gar nicht mehr abgefragt)."""
import pytest
from playwright.sync_api import expect
from uihelpers import simulate_running

FAKE_NOTIFICATION = """
window.__notes = [];
window.Notification = class {
  constructor(title, opts) { window.__notes.push(title + ": " + (opts && opts.body)); }
  close() {}
};
window.Notification.permission = "granted";
window.Notification.requestPermission = async () => "granted";
window.__hidden = false;
Object.defineProperty(document, "hidden", { configurable: true, get: () => window.__hidden });
Object.defineProperty(document, "visibilityState", { configurable: true,
  get: () => (window.__hidden ? "hidden" : "visible") });
try { localStorage.setItem("md_notify", "1"); } catch (e) {}
"""


def _set_hidden(page, hidden: bool):
    page.evaluate("(h) => { window.__hidden = h; document.dispatchEvent(new Event('visibilitychange')); }",
                  hidden)


@pytest.fixture()
def server_page(page, make_instance):
    inst = make_instance("Survival-SMP")
    sim = {inst["id"]: {"running": True, "name": inst["name"], "players": []}}
    page.clock.install()
    page.add_init_script(FAKE_NOTIFICATION)
    simulate_running(page, sim)
    page.goto("/")
    expect(page.locator("#ov-summary")).to_contain_text("1 von 1 Server läuft")
    page.clock.run_for(16_000)  # erster Live-Ping → Ausgangszustand gemerkt
    return page, sim[inst["id"]]


def test_absturz_im_hintergrund_als_desktop_meldung(server_page):
    page, sim = server_page
    _set_hidden(page, True)
    sim["running"] = False
    page.clock.run_for(31_000)
    page.wait_for_function("() => window.__notes.length > 0", timeout=5000)
    assert page.evaluate("window.__notes") == ["Survival-SMP · Minedocker: ist abgestürzt"]


def test_beitritt_im_hintergrund_mit_namen(server_page):
    page, sim = server_page
    _set_hidden(page, True)
    sim["players"] = ["Steve"]
    page.clock.run_for(31_000)
    page.wait_for_function("() => window.__notes.length > 0", timeout=5000)
    assert page.evaluate("window.__notes") == [
        "Survival-SMP · Minedocker: Steve ist beigetreten (1 online)"]


def test_sichtbarer_tab_zeigt_toast_statt_desktop(server_page):
    page, sim = server_page
    sim["running"] = False
    page.clock.run_for(6_000)
    expect(page.locator("#toasts")).to_contain_text("Survival-SMP: ist abgestürzt")
    assert page.evaluate("window.__notes") == []


def test_ohne_benachrichtigungen_kein_hintergrund_polling(page, make_instance):
    inst = make_instance("Survival-SMP")
    calls = {"n": 0}
    page.clock.install()
    simulate_running(page, {inst["id"]: {"running": True, "name": inst["name"]}})

    def count(route):
        calls["n"] += 1
        route.fallback()

    page.route("**/api/instances", count)
    page.goto("/")
    expect(page.locator("#ov-summary")).to_contain_text("1 von 1 Server läuft")
    page.evaluate("""() => {
      Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
      document.dispatchEvent(new Event("visibilitychange"));
    }""")
    before = calls["n"]
    page.clock.run_for(65_000)
    assert calls["n"] == before
