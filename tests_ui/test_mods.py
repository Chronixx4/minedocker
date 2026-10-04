"""Mod-Liste: Infos aus der .jar, erkannte Probleme und Papierkorb."""
import io
import json
import zipfile

from playwright.sync_api import expect


def _jar(mod_id, name, version="1.0.0", env="*", depends=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("fabric.mod.json", json.dumps({
            "schemaVersion": 1, "id": mod_id, "name": name, "version": version,
            "environment": env, "depends": depends or {}}))
    return buf.getvalue()


def test_mod_liste_zeigt_namen_probleme_und_papierkorb(page, make_instance, data_dir):
    inst = make_instance("Modded")
    mods = data_dir / "instances" / inst["id"] / "mods"
    (mods / "create-fabric-0.5.1-build.1631+mc1.21.4.jar").write_bytes(
        _jar("create", "Create Fabric", "0.5.1", depends={"fabric-api": ">=0.92"}))
    (mods / "iris-fabric-1.8.0.jar").write_bytes(_jar("iris", "Iris Shaders", env="client"))
    (mods / "lithium-fabric-0.15.1.jar").write_bytes(_jar("lithium", "Lithium", "0.15.1"))

    page.goto("/")
    page.locator(f'.ov-card[data-id="{inst["id"]}"] .ov-card-title').click()
    page.keyboard.press("4")
    rows = page.locator("#detail-mods .mod-row")
    expect(rows).to_have_count(3)
    expect(rows.nth(0).locator(".mod-name")).to_have_text("Create Fabric")
    expect(rows.nth(0).locator(".mod-meta")).to_contain_text("0.5.1")
    expect(rows.nth(0).locator(".mod-tag")).to_have_text("Abhängigkeit fehlt")
    expect(rows.nth(1).locator(".mod-tag")).to_have_text("nur Client")
    expect(rows.nth(2).locator(".mod-tag")).to_have_count(0)
    expect(page.locator("#mods-problems")).to_be_visible()
    expect(page.locator("#mods-problems-title")).to_have_text("2 Probleme erkannt:")
    expect(page.locator("#mods-problems-list")).to_contain_text("fabric-api (>=0.92)")
    expect(page.locator("#mods-restart-note")).to_be_hidden()

    # Filter greift auch auf den Mod-Namen aus der .jar
    page.fill("#mods-filter-input", "shaders")
    expect(rows).to_have_count(1)
    page.fill("#mods-filter-input", "")

    # Löschen → Papierkorb → Wiederherstellen
    page.on("dialog", lambda d: d.accept())
    expect(page.locator("#mods-trash")).to_be_hidden()
    rows.nth(1).locator(".delete").click()
    expect(rows).to_have_count(2)
    expect(page.locator("#mods-problems-title")).to_have_text("1 Problem erkannt:")
    trash = page.locator("#mods-trash")
    expect(trash).to_be_visible()
    trash.locator("summary").click()
    expect(page.locator("#mods-trash-list .mod-row")).to_have_count(1)
    expect(page.locator("#mods-trash-list")).to_contain_text("iris-fabric-1.8.0.jar")
    page.locator("#mods-trash-list .mod-row button").click()
    expect(rows).to_have_count(3)
    expect(trash).to_be_hidden()
    assert (mods / "iris-fabric-1.8.0.jar").is_file()
