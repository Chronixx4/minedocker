"""Modpack-Upload: Server Files ablegen → Vorschau → neuer Server."""
import io
import zipfile

from playwright.sync_api import expect


def _server_files() -> bytes:
    """Nachbau der ATM10-„Server Files“ (ohne manifest.json)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Server-Files-4.11/variables.txt",
                    "MINECRAFT_VERSION=1.21.1\nMODLOADER=NeoForge\n"
                    "MODLOADER_VERSION=21.1.209\n")
        zf.writestr("Server-Files-4.11/startserver.sh", "#!/bin/sh\n")
        zf.writestr("Server-Files-4.11/mods/allthemodium.jar", b"jar")
        zf.writestr("Server-Files-4.11/config/a.toml", "a = 1\n")
    return buf.getvalue()


def test_server_files_vorschau_und_neuer_server(page, api):
    page.goto("/")
    page.locator('.tab[data-tab="upload"]').click()
    expect(page.locator("#up-drop")).to_be_visible()
    page.locator("#up-file").set_input_files({
        "name": "ServerFiles-4.11.zip", "mimeType": "application/zip",
        "buffer": _server_files()})

    preview = page.locator("#up-preview")
    expect(preview).to_be_visible()
    expect(page.locator("#up-pv-title")).to_have_text("Server-Files-4.11")
    facts = page.locator("#up-pv-facts")
    expect(facts).to_contain_text("„Server Files“")
    expect(facts).to_contain_text("1.21.1")
    expect(facts).to_contain_text("neoforge 21.1.209")
    expect(facts).to_contain_text("Java 21")
    expect(page.locator("#up-memory")).to_have_value("4G")
    expect(page.locator("#up-drop")).to_be_hidden()

    page.locator("#up-eula").check()
    page.locator("#up-install-btn").click()
    expect(page.locator("#up-summary-text")).to_contain_text(
        "Neuer Server „Server-Files-4.11“", timeout=15000)
    expect(page.locator("#up-summary-text")).to_contain_text("1 Dateien installiert")

    instances = api.get("/api/instances").json()["instances"]
    assert len(instances) == 1
    inst = instances[0]
    assert (inst["loader"], inst["game_version"]) == ("neoforge", "1.21.1")


def test_unbekanntes_archiv_zeigt_fehler(page):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("README.txt", "nix")
    page.goto("/")
    page.locator('.tab[data-tab="upload"]').click()
    page.locator("#up-file").set_input_files({
        "name": "kaputt.zip", "mimeType": "application/zip",
        "buffer": buf.getvalue()})
    expect(page.locator("#up-error")).to_contain_text("Kein Modpack erkannt")
    expect(page.locator("#up-preview")).to_be_hidden()
    expect(page.locator("#up-drop")).to_be_visible()


def test_server_detail_springt_zum_upload_mit_ziel(page, make_instance):
    inst = make_instance("Ziel")
    page.goto("/")
    page.locator(f'.ov-card[data-id="{inst["id"]}"] .ov-card-title').click()
    page.keyboard.press("4")
    page.locator("#ws-mods-seg .seg-btn", has_text="Modpack").click()
    page.locator("#pack-upload-goto").click()
    expect(page.locator("#tab-upload")).to_be_visible()
    expect(page.locator("#up-target-select")).to_have_value(inst["id"])
