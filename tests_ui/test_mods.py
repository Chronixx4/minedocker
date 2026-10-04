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


def _open_mods(page, inst, view):
    page.goto("/")
    page.locator(f'.ov-card[data-id="{inst["id"]}"] .ov-card-title').click()
    page.keyboard.press("4")
    page.locator("#ws-mods-seg .seg-btn", has_text=view).click()


def test_mod_liste_zeigt_namen_probleme_und_papierkorb(page, make_instance, data_dir):
    inst = make_instance("Modded")
    mods = data_dir / "instances" / inst["id"] / "mods"
    (mods / "create-fabric-0.5.1-build.1631+mc1.21.4.jar").write_bytes(
        _jar("create", "Create Fabric", "0.5.1", depends={"fabric-api": ">=0.92"}))
    (mods / "iris-fabric-1.8.0.jar").write_bytes(_jar("iris", "Iris Shaders", env="client"))
    (mods / "lithium-fabric-0.15.1.jar").write_bytes(_jar("lithium", "Lithium", "0.15.1"))

    _open_mods(page, inst, "Installiert")
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

    # Löschen (eigene Rückfrage) → Papierkorb → Wiederherstellen
    expect(page.locator("#mods-trash")).to_be_hidden()
    rows.nth(1).locator(".delete").click()
    expect(page.locator("#confirm-dialog")).to_be_visible()
    expect(page.locator("#cd-list")).to_contain_text("iris-fabric-1.8.0.jar")
    page.locator("#cd-ok").click()
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


HITS = [
    {"project_id": "gvQqBUqZ", "slug": "lithium", "title": "Lithium", "author": "jellysquid3",
     "description": "Server-Optimierung", "downloads": 48000000, "icon_url": "",
     "latest_version": "0.15.1", "server_side": "optional", "installed": False},
    {"project_id": "AANobbMI", "slug": "sodium", "title": "Sodium", "author": "jellysquid3",
     "description": "Rendering", "downloads": 90000000, "icon_url": "",
     "latest_version": "0.6.0", "server_side": "unsupported", "installed": False},
]

BROWSE = {
    "source": "modrinth", "filtered": True, "loader": "fabric", "game_version": "1.21.4",
    "project": {"project_id": "gvQqBUqZ", "title": "Lithium", "description": "Server-Optimierung",
                "icon_url": "", "page_url": "https://modrinth.com/mod/lithium",
                "downloads": 48000000, "categories": ["optimization"],
                "server_side": "optional", "client_side": "optional"},
    "versions": [
        {"id": "v2", "version_number": "0.15.1", "name": "", "type": "release",
         "date": "2026-09-28T00:00:00Z", "game_versions": ["1.21.4"], "loaders": ["fabric"],
         "filename": "lithium-0.15.1.jar", "size": 700000, "changelog": "Hopper-Fix",
         "required_dependencies": 0},
        {"id": "v1", "version_number": "0.15.0-beta.2", "name": "", "type": "beta",
         "date": "2026-08-01T00:00:00Z", "game_versions": ["1.21.4"], "loaders": ["fabric"],
         "filename": "lithium-0.15.0.jar", "size": 690000, "changelog": "Beta",
         "required_dependencies": 1},
    ],
}


def _plan(items):
    deps = [{"filename": "fabric-api-0.105.jar", "size": 2000000, "project_id": "P7dR8mSH",
             "source": "modrinth"}] if any(i.get("version_id") == "v1" or
                                           i["project_id"] == "AANobbMI" for i in items) else []
    return {"items": [{"filename": f"{i['project_id']}.jar", "size": 700000, **i}
                      for i in items],
            "dependencies": deps, "skipped": [], "errors": [], "conflicts": [],
            "total_size": 700000 * len(items) + sum(d["size"] for d in deps)}


def test_modbrowser_im_server_bereich(page, make_instance):
    inst = make_instance("Browser-SMP")
    calls = {"plan": [], "install": [], "search": []}

    def search(route):
        calls["search"].append(route.request.url)
        route.fulfill(json={"total": 2, "hits": HITS, "loader": "fabric",
                            "game_version": "1.21.4"})

    def plan(route):
        body = route.request.post_data_json
        calls["plan"].append(body)
        route.fulfill(json=_plan(body["items"]))

    def install(route):
        body = route.request.post_data_json
        calls["install"].append(body)
        route.fulfill(json={"job_id": "job1", "total": 100, "items": [
            {"filename": f"{i['project_id']}.jar", "project_id": i["project_id"],
             "source": "modrinth"} for i in body["items"]],
            "dependencies": [], "skipped": [], "errors": []})

    page.route("**/api/modrinth/search?*", search)
    page.route(f"**/api/instances/{inst['id']}/mods/browse/modrinth/gvQqBUqZ*",
               lambda r: r.fulfill(json=BROWSE))
    page.route(f"**/api/instances/{inst['id']}/mods/plan", plan)
    page.route(f"**/api/instances/{inst['id']}/mods/install", install)
    page.route("**/api/modrinth/jobs/job1", lambda r: r.fulfill(json={
        "id": "job1", "status": "done", "downloaded": 100, "total": 100, "bundle": []}))

    _open_mods(page, inst, "Hinzufügen")
    card = page.locator("#ws-mods-add #search-card")
    expect(card).to_be_visible()
    expect(card.locator(".search-target-pick")).to_be_hidden()
    expect(page.locator("#search-target")).to_contain_text("Browser-SMP")
    results = page.locator("#search-results .result")
    expect(results).to_have_count(2)
    assert "server_ok" in calls["search"][-1]  # Standard: keine reinen Client-Mods
    assert f"instance_id={inst['id']}" in calls["search"][-1]
    expect(results.nth(0).locator(".side")).to_have_text("Server: optional")
    expect(results.nth(1).locator(".side")).to_have_text("Nur Client")

    # Details: Versionen, Changelog, Abhängigkeits-Vorschau je Version
    results.nth(0).locator(".details").click()
    dialog = page.locator("#mod-detail")
    expect(dialog).to_be_visible()
    expect(page.locator("#md-link")).to_have_attribute("href", "https://modrinth.com/mod/lithium")
    expect(page.locator("#md-versions-title")).to_have_text("Versionen für fabric 1.21.4")
    expect(page.locator("#md-versions .md-version")).to_have_count(2)
    expect(page.locator("#md-loading")).to_be_hidden()
    expect(page.locator("#md-changelog")).to_have_text("Hopper-Fix")
    expect(page.locator("#md-plan")).to_contain_text("Keine zusätzlichen")
    page.locator("#md-versions .md-version").nth(1).click()
    expect(page.locator("#md-changelog")).to_have_text("Beta")
    expect(page.locator("#md-plan")).to_contain_text("fabric-api-0.105.jar")
    assert calls["plan"][-1]["items"][0]["version_id"] == "v1"
    expect(page.locator("#md-install")).to_have_text("0.15.0-beta.2 installieren")
    page.locator("#md-cart").click()
    expect(dialog).to_be_hidden()

    # Vormerken: Lithium (Beta-Version aus den Details) + Sodium per Häkchen
    expect(results.nth(0).locator(".pick")).to_be_checked()
    results.nth(1).locator(".pick").check()
    cart = page.locator("#mod-cart")
    expect(cart).to_be_visible()
    expect(page.locator("#mod-cart-title")).to_have_text("2 Mods vorgemerkt")
    expect(page.locator("#mod-cart-detail")).to_contain_text("1 Abhängigkeit kommt dazu")

    # Gemeinsam installieren: Rückfrage wegen der reinen Client-Mod
    page.locator("#mod-cart-install").click()
    expect(page.locator("#confirm-dialog")).to_be_visible()
    expect(page.locator("#cd-list")).to_have_text("Sodium")
    page.locator("#cd-ok").click()
    expect(cart).to_be_hidden()
    items = calls["install"][-1]["items"]
    assert [(i["project_id"], i["version_id"]) for i in items] == \
        [("gvQqBUqZ", "v1"), ("AANobbMI", None)]
    expect(results.nth(0).locator(".install")).to_have_text("Installiert ✓")
    expect(results.nth(1).locator(".install")).to_have_text("Installiert ✓")

    # Tab „Mods suchen“ leiht sich die Suche aus, der Server-Bereich holt sie zurück
    page.locator('.tab[data-tab="search"]').click()
    expect(page.locator("#tab-search #search-card")).to_be_visible()
    page.locator('.tab[data-tab="servers"]').click()
    expect(page.locator("#ws-mods-add #search-card")).to_be_visible()

    # Zurück zur installierten Liste
    page.locator("#ws-mods-seg .seg-btn", has_text="Installiert").click()
    expect(page.locator("#ws-mods-installed")).to_be_visible()
    expect(page.locator("#ws-mods-add")).to_be_hidden()


def test_mods_suchen_tab_hat_zielauswahl(page, make_instance):
    make_instance("Tab-Ziel")
    page.route("**/api/modrinth/search?*", lambda r: r.fulfill(json={
        "total": 0, "hits": [], "loader": "fabric", "game_version": "1.21.4"}))
    page.goto("/")
    page.locator('.tab[data-tab="search"]').click()
    card = page.locator("#tab-search #search-card")
    expect(card).to_be_visible()
    expect(card.locator(".search-target-pick")).to_be_visible()
    expect(page.locator("#search-target-select")).to_contain_text("Tab-Ziel")
