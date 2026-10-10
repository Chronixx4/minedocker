"""Tests für die Inventar-Ansicht: SNBT-Parser, Slot-Zuordnung je MC-Format,
Befehlsbau, RCON über eine Verbindung, API und Item-Icons aus Mod-JARs."""
import io
import json
import re
import shutil
import socket
import struct
import threading
import zipfile

import pytest

from app import instances, itemassets, runtime
from app import inventory as inv
from app import rcon as rcon_mod
from app.config import settings

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10"
       b"\x08\x06\x00\x00\x00\x1f\xf3\xffa")


@pytest.fixture(autouse=True)
def _leere_instanzen(monkeypatch):
    # Nie echte Mojang-Downloads in Tests
    monkeypatch.setattr(settings, "item_icons_vanilla", False)
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    itemassets.reset_for_tests()
    yield
    itemassets.reset_for_tests()


def data(player, snbt):
    return f"{player} has the following entity data: {snbt}"


NEU = data("Chronixx", '[{Slot: 0b, components: {"minecraft:damage": 157, '
                       '"minecraft:enchantments": {levels: {"minecraft:sharpness": 5}}}, '
                       'count: 1, id: "minecraft:netherite_sword"}, '
                       '{Slot: 9b, count: 48, id: "mekanism:ingot_osmium"}, '
                       '{Slot: 100b, count: 1, id: "minecraft:iron_boots"}, '
                       '{Slot: -106b, count: 1, id: "minecraft:shield"}]')
ALT = data("Steve", '[{Slot: 0b, id: "minecraft:diamond_sword", Count: 1b, '
                    'tag: {Damage: 3, Enchantments: [{id: "minecraft:sharpness", lvl: 5s}], '
                    'display: {Name: \'{"text":"Klinge"}\'}}}, '
                    '{Slot: 103b, id: "minecraft:iron_helmet", Count: 1b}]')
LEER = "Found no elements matching x"


def outputs(inventory, *, ender=LEER, equipment=LEER, extra=()):
    stats = [data("P", "3"), data("P", "18.0f"), data("P", "17"), data("P", "34"),
             data("P", "[214.3d, 71.0d, -388.5d]"), data("P", '"minecraft:overworld"'),
             data("P", "0")]
    return [inventory, ender, equipment, *stats, *extra]


# ---------------------------------------------------------------------------
# SNBT
# ---------------------------------------------------------------------------

class TestSnbt:
    def test_rundreise_behaelt_typen(self):
        text = ('{a:1b,b:2.5f,"minecraft:x":[I;1,2,3],s:"zit\\"at",l:[],'
                'n:{},t:-106b,u:[L;4l]}')
        tree = inv.parse_snbt(text)
        assert inv.to_snbt(tree) == text
        assert tree["s"] == 'zit"at'
        assert tree["t"].number() == -106

    def test_einfache_anfuehrungszeichen(self):
        tree = inv.parse_snbt("{name:'{\"text\":\"Hi\"}'}")
        assert tree["name"] == '{"text":"Hi"}'

    def test_fehler(self):
        with pytest.raises(inv.SnbtError):
            inv.parse_snbt("{a:1")
        with pytest.raises(inv.SnbtError):
            inv.parse_snbt("{a:1} rest")

    def test_data_get(self):
        assert inv.parse_data_get(data("P", "[]")) == []
        assert inv.parse_data_get(LEER) is None
        with pytest.raises(inv.PlayerOffline):
            inv.parse_data_get("No entity was found")


# ---------------------------------------------------------------------------
# Slots je Format
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_format_ab_1_20_5(self):
        snap, slots = inv.build_snapshot("Chronixx", "neoforge", outputs(NEU))
        assert set(snap["slots"]) == {"container.0", "container.9", "armor.feet",
                                      "weapon.offhand"}
        sword = snap["slots"]["container.0"]
        assert sword["damage"] == 157
        assert sword["enchantments"] == [{"id": "minecraft:sharpness", "level": 5}]
        assert snap["slots"]["container.9"]["count"] == 48
        assert snap["stats"] == {"health": 18.0, "food": 17, "level": 34,
                                 "pos": [214, 71, -388], "dimension": "minecraft:overworld",
                                 "gamemode": "survival"}
        assert snap["selected_slot"] == 3
        assert sword["sig"] == inv.signature(slots["container.0"])

    def test_format_bis_1_20_4(self):
        snap, _ = inv.build_snapshot("Steve", "forge", outputs(ALT))
        sword = snap["slots"]["container.0"]
        assert sword["custom_name"] == "Klinge"
        assert sword["damage"] == 3
        assert sword["enchantments"] == [{"id": "minecraft:sharpness", "level": 5}]
        assert "armor.head" in snap["slots"]

    def test_equipment_ab_1_21_5(self):
        equipment = data("P", '{head: {count: 1, id: "minecraft:netherite_helmet"}, '
                              'offhand: {count: 7, id: "minecraft:torch"}, '
                              'body: {count: 1, id: "minecraft:saddle"}}')
        snap, _ = inv.build_snapshot("P", "fabric", outputs(data("P", "[]"), equipment=equipment))
        assert snap["slots"]["armor.head"]["id"] == "minecraft:netherite_helmet"
        assert snap["slots"]["weapon.offhand"]["count"] == 7
        assert len(snap["slots"]) == 2

    def test_endertruhe(self):
        ender = data("P", '[{Slot: 26b, count: 2, id: "minecraft:diamond"}]')
        snap, _ = inv.build_snapshot("P", "fabric", outputs(data("P", "[]"), ender=ender))
        assert snap["slots"]["enderchest.26"]["count"] == 2

    def test_curios_nur_ansehen(self):
        curios = data("P", '{"curios:inventory": {Curios: [{Identifier: "ring", '
                           'StacksHandler: {Stacks: {Items: [{Slot: 0, count: 1, '
                           'id: "artifacts:gold_ring"}]}}}]}}')
        snap, _ = inv.build_snapshot("P", "neoforge", outputs(NEU, extra=[curios]))
        assert snap["mod_items"][0]["id"] == "artifacts:gold_ring"
        assert snap["mod_items"][0]["group"] == "ring"
        assert snap["mod_items"][0]["movable"] is False
        assert "artifacts:gold_ring" not in {i["id"] for i in snap["slots"].values()}

    def test_offline(self):
        with pytest.raises(inv.PlayerOffline):
            inv.build_snapshot("P", "fabric", ["No entity was found", *outputs(NEU)[1:]])


# ---------------------------------------------------------------------------
# Befehle
# ---------------------------------------------------------------------------

class TestBefehle:
    def test_verschieben_mit_komponenten(self):
        _, slots = inv.build_snapshot("Chronixx", "neoforge", outputs(NEU))
        cmds = inv.plan_action("Chronixx", "move", slots, "container.0", "container.5", None, None)
        assert cmds == [
            'item replace entity Chronixx container.5 with minecraft:netherite_sword'
            '[minecraft:damage=157,minecraft:enchantments={levels:{"minecraft:sharpness":5}}] 1',
            "item replace entity Chronixx container.0 with minecraft:air",
        ]

    def test_tauschen(self):
        _, slots = inv.build_snapshot("Chronixx", "neoforge", outputs(NEU))
        cmds = inv.plan_action("Chronixx", "move", slots, "container.9", "weapon.offhand", None, None)
        assert cmds[0] == "item replace entity Chronixx weapon.offhand with mekanism:ingot_osmium 48"
        assert cmds[1] == "item replace entity Chronixx container.9 with minecraft:shield 1"

    def test_altes_format_mit_tag(self):
        _, slots = inv.build_snapshot("Steve", "forge", outputs(ALT))
        cmd = inv.plan_action("Steve", "set_count", slots, "container.0", None, 2, None)[0]
        assert cmd.startswith("item replace entity Steve container.0 with minecraft:diamond_sword{Damage:3,")
        assert cmd.endswith("} 2")

    def test_entfernte_komponente(self):
        item = inv.parse_snbt('{count: 1, id: "minecraft:apple", components: {"!minecraft:food": {}}}')
        assert inv.item_command_string(item) == "minecraft:apple[!minecraft:food]"

    def test_leerer_slot(self):
        with pytest.raises(ValueError):
            inv.plan_action("P", "set_count", {}, "container.0", None, 3, None)

    def test_zu_grosse_items(self):
        big = inv.parse_snbt('{count: 1, id: "minecraft:written_book", components: '
                             '{"minecraft:custom_data": {x: "' + "a" * 2000 + '"}}}')
        assert inv.describe_item(big, "container.0")["movable"] is False
        with pytest.raises(ValueError):
            inv.plan_action("P", "move", {"container.0": big}, "container.0", "container.1", None, None)

    def test_versionen(self):
        assert inv.editing_supported("1.21.1")
        assert inv.editing_supported("1.17")
        assert not inv.editing_supported("1.16.5")
        assert inv.editing_supported("24w14a")

    def test_slot_pruefung(self):
        assert inv.SLOT_RE.match("container.35")
        assert inv.SLOT_RE.match("enderchest.26")
        assert not inv.SLOT_RE.match("container.36")
        assert not inv.SLOT_RE.match("container.0 with minecraft:tnt")


# ---------------------------------------------------------------------------
# RCON: mehrere Befehle über eine Verbindung
# ---------------------------------------------------------------------------

def _fake_rcon_server(answers):
    """Mini-Server nach Vanilla-Art: lange Antworten in 4096-Byte-Paketen,
    unbekannte Pakettypen → 'Unknown request'."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def send(conn, rid, ptype, payload):
        body = struct.pack("<ii", rid, ptype) + payload + b"\x00\x00"
        conn.sendall(struct.pack("<i", len(body)) + body)

    def run():
        conn, _ = srv.accept()
        with conn:
            while True:
                head = conn.recv(4)
                if len(head) < 4:
                    return
                (length,) = struct.unpack("<i", head)
                body = b""
                while len(body) < length:
                    body += conn.recv(length - len(body))
                rid, ptype = struct.unpack("<ii", body[:8])
                text = body[8:-2].decode()
                if ptype == 3:
                    send(conn, rid, 2, b"")
                elif ptype == 2:
                    out = answers(text).encode()
                    for i in range(0, max(len(out), 1), 4096):
                        send(conn, rid, 0, out[i:i + 4096])
                else:
                    send(conn, rid, 0, b"Unknown request 0")

    threading.Thread(target=run, daemon=True).start()
    return srv


def test_rcon_commands_mehrteilige_antwort():
    long_text = "x" * 9000 + "ä"
    srv = _fake_rcon_server(lambda cmd: long_text if cmd == "lang" else f"ok:{cmd}")
    try:
        port = srv.getsockname()[1]
        out = rcon_mod.commands("127.0.0.1", port, "pw", ["a", "lang", "b"], timeout=2)
    finally:
        srv.close()
    assert out == ["ok:a", long_text, "ok:b"]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class FakeSpieler:
    """Simuliertes Inventar hinter RCON (nur Items ohne Komponenten)."""

    def __init__(self):
        self.slots = {0: ("minecraft:diamond_sword", 1), 9: ("minecraft:cobblestone", 64)}
        self.sent = []
        self.online = True

    def answer(self, cmd):
        self.sent.append(cmd)
        if not self.online:
            return "No entity was found"
        m = re.match(r'^data get entity (\w+) "?([^"\s]+)"?$', cmd)
        if m:
            field = m.group(2)
            if field == "Inventory":
                items = ", ".join(f'{{Slot: {s}b, count: {c}, id: "{i}"}}'
                                  for s, (i, c) in sorted(self.slots.items()))
                return data(m.group(1), f"[{items}]")
            if field in ("Health", "foodLevel", "XpLevel", "SelectedItemSlot"):
                return data(m.group(1), "1")
            return f"Found no elements matching {field}"
        m = re.match(r"^item replace entity (\w+) container\.(\d+) with (\S+?)(?: (\d+))?$", cmd)
        if m:
            slot, item, count = int(m.group(2)), m.group(3), m.group(4)
            if item == "minecraft:air":
                self.slots.pop(slot, None)
            else:
                self.slots[slot] = (item, int(count))
            return f"Replaced a slot on {m.group(1)} with [{item}]"
        if cmd.startswith("give "):
            return "Unknown item 'foo:bar'" if "foo:bar" in cmd else "Gave 5 [Stone] to Steve"
        return "?"


@pytest.fixture()
def spiel(monkeypatch):
    fake = FakeSpieler()
    monkeypatch.setattr(runtime, "is_running", lambda inst: True)
    monkeypatch.setattr(rcon_mod, "commands",
                        lambda h, p, pw, cmds, timeout=5.0: [fake.answer(c) for c in cmds])
    return fake


@pytest.fixture()
def instanz():
    return instances.create_instance("Inv-Srv", "neoforge", "1.21.1", accept_eula=True)


def url(inst, player="Steve"):
    return f"/api/instances/{inst['id']}/players/{player}/inventory"


class TestApi:
    def test_lesen(self, client, instanz, spiel):
        r = client.get(url(instanz))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["slots"]["container.0"]["id"] == "minecraft:diamond_sword"
        assert body["editable"] is True
        assert body["icons"] == "disabled"
        assert 'data get entity Steve "neoforge:attachments"' in spiel.sent

    def test_gestoppt(self, client, instanz):
        assert client.get(url(instanz)).status_code == 409

    def test_offline_404(self, client, instanz, spiel):
        spiel.online = False
        assert client.get(url(instanz)).status_code == 404

    def test_ungueltiger_name(self, client, instanz, spiel):
        assert client.get(url(instanz, "a;b")).status_code in (400, 404)
        assert client.get(url(instanz, "x" * 17)).status_code == 400

    def test_verschieben(self, client, instanz, spiel):
        snap = client.get(url(instanz)).json()
        r = client.post(url(instanz), json={
            "action": "move", "slot": "container.0", "to": "container.5",
            "expect": {"container.0": snap["slots"]["container.0"]["sig"], "container.5": ""}})
        assert r.status_code == 200, r.text
        assert spiel.slots == {5: ("minecraft:diamond_sword", 1), 9: ("minecraft:cobblestone", 64)}
        assert r.json()["slots"]["container.5"]["id"] == "minecraft:diamond_sword"

    def test_geaendert_409(self, client, instanz, spiel):
        snap = client.get(url(instanz)).json()
        spiel.slots[9] = ("minecraft:cobblestone", 10)  # Spieler war schneller
        r = client.post(url(instanz), json={
            "action": "set_count", "slot": "container.9", "count": 32,
            "expect": {"container.9": snap["slots"]["container.9"]["sig"]}})
        assert r.status_code == 409
        assert spiel.slots[9] == ("minecraft:cobblestone", 10)
        assert not any(c.startswith("item replace") for c in spiel.sent)

    def test_menge_und_loeschen(self, client, instanz, spiel):
        r = client.post(url(instanz), json={"action": "set_count", "slot": "container.9", "count": 5})
        assert r.status_code == 200
        assert spiel.slots[9] == ("minecraft:cobblestone", 5)
        r = client.post(url(instanz), json={"action": "clear", "slot": "container.9"})
        assert r.status_code == 200
        assert 9 not in spiel.slots

    def test_geben(self, client, instanz, spiel):
        r = client.post(url(instanz), json={"action": "give", "item_id": "minecraft:stone", "count": 5})
        assert r.status_code == 200
        assert "give Steve minecraft:stone 5" in spiel.sent
        r = client.post(url(instanz), json={"action": "give", "item_id": "foo:bar", "count": 1})
        assert r.status_code == 400
        assert "Unknown item" in r.json()["detail"]

    @pytest.mark.parametrize("body", [
        {"action": "give", "item_id": "stone; op Steve", "count": 1},
        {"action": "give", "item_id": "minecraft:stone", "count": 0},
        {"action": "set_count", "slot": "container.0", "count": 100},
        {"action": "move", "slot": "container.0", "to": "container.0"},
        {"action": "clear", "slot": "container.0 with minecraft:tnt"},
        {"action": "run", "slot": "container.0"},
    ])
    def test_eingaben_geprueft(self, client, instanz, spiel, body):
        r = client.post(url(instanz), json=body)
        assert r.status_code in (400, 422), body
        assert not any(c.startswith(("item ", "give ")) for c in spiel.sent)

    def test_alte_version_nur_lesen(self, client, spiel):
        alt = instances.create_instance("Alt-Srv", "forge", "1.16.5", accept_eula=True)
        assert client.get(url(alt)).json()["editable"] is False
        r = client.post(url(alt), json={"action": "clear", "slot": "container.0"})
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# Icons und Namen aus Mod-JARs
# ---------------------------------------------------------------------------

def _mod_jar(path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("assets/demo/lang/en_us.json", json.dumps({
            "item.demo.ingot_osmium": "Osmium Ingot", "block.demo.block_osmium": "Block of Osmium"}))
        zf.writestr("assets/demo/lang/de_de.json", json.dumps({"item.demo.ingot_osmium": "Osmiumbarren",
                                                              "enchantment.demo.glow": "Leuchten"}))
        zf.writestr("assets/demo/models/item/ingot_osmium.json",
                    json.dumps({"parent": "item/generated", "textures": {"layer0": "demo:item/ingot_osmium"}}))
        zf.writestr("assets/demo/textures/item/ingot_osmium.png", PNG + b"ingot")
        zf.writestr("assets/demo/models/item/block_osmium.json", json.dumps({"parent": "demo:block/block_osmium"}))
        zf.writestr("assets/demo/models/block/block_osmium.json",
                    json.dumps({"parent": "block/cube_all", "textures": {"all": "demo:block/osmium"}}))
        zf.writestr("assets/demo/textures/block/osmium.png", PNG + b"block")
    path.write_bytes(buf.getvalue())


class TestItemIcons:
    def test_namen_icons_suche(self, client, instanz):
        mods = instances.mods_dir(instanz["id"])
        mods.mkdir(parents=True, exist_ok=True)
        _mod_jar(mods / "demo.jar")
        base = f"/api/instances/{instanz['id']}/items"
        r = client.get(f"{base}?ids=demo:ingot_osmium,demo:block_osmium,minecraft:stone,demo:glow")
        assert r.status_code == 200
        items = r.json()["items"]
        assert items["demo:ingot_osmium"]["name"] == "Osmiumbarren"
        assert items["demo:block_osmium"]["name"] == "Block of Osmium"
        assert items["minecraft:stone"]["name"] is None
        assert items["demo:glow"]["name"] == "Leuchten"

        r = client.get(f"{base}/icon", params={"id": "demo:ingot_osmium"})
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content.endswith(b"ingot")
        r = client.get(f"{base}/icon", params={"id": "demo:block_osmium"})
        assert r.content.endswith(b"block")
        assert client.get(f"{base}/icon", params={"id": "minecraft:stone"}).status_code == 404
        assert client.get(f"{base}/icon", params={"id": "../x"}).status_code == 400

        results = client.get(base, params={"q": "osmium"}).json()["results"]
        assert {x["id"] for x in results} == {"demo:ingot_osmium", "demo:block_osmium"}

    def test_index_neu_bei_neuer_jar(self, instanz):
        mods = instances.mods_dir(instanz["id"])
        mods.mkdir(parents=True, exist_ok=True)
        first = itemassets.index_for(instanz)
        assert first.items == set()
        _mod_jar(mods / "demo.jar")
        assert "demo:ingot_osmium" in itemassets.index_for(instanz).items

    def test_vanilla_aus(self):
        assert itemassets.ensure_vanilla("1.21.1") == "disabled"


def test_vanilla_download_teilmenge(monkeypatch, tmp_path):
    """Download über einen Mock-Transport: nur benötigte Dateien landen im
    Cache, deutsche Namen kommen aus dem Asset-Index."""
    import httpx

    monkeypatch.setattr(settings, "item_icons_vanilla", True)
    monkeypatch.setattr(settings, "instances_dir", tmp_path)
    jar = io.BytesIO()
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("assets/minecraft/models/item/stone.json", json.dumps({"parent": "minecraft:block/stone"}))
        zf.writestr("assets/minecraft/models/block/stone.json",
                    json.dumps({"parent": "block/cube_all", "textures": {"all": "minecraft:block/stone"}}))
        zf.writestr("assets/minecraft/textures/block/stone.png", PNG + b"stone")
        zf.writestr("assets/minecraft/lang/en_us.json", json.dumps({"block.minecraft.stone": "Stone"}))
        zf.writestr("net/minecraft/Main.class", b"\x00" * 100)
        zf.writestr("assets/minecraft/sounds.json", "{}")
    lang_hash = "a" * 40

    def handler(request):
        u = str(request.url)
        if u.endswith("version_manifest_v2.json"):
            return httpx.Response(200, json={"versions": [{"id": "1.21.1", "url": "https://x/v.json"}]})
        if u == "https://x/v.json":
            return httpx.Response(200, json={"downloads": {"client": {"url": "https://x/client.jar"}},
                                             "assetIndex": {"url": "https://x/index.json"}})
        if u == "https://x/client.jar":
            return httpx.Response(200, content=jar.getvalue())
        if u == "https://x/index.json":
            return httpx.Response(200, json={"objects": {"minecraft/lang/de_de.json": {"hash": lang_hash}}})
        if u.endswith(f"/aa/{lang_hash}"):
            return httpx.Response(200, json={"block.minecraft.stone": "Stein"})
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(itemassets.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    itemassets._download_vanilla("1.21.1")
    assert itemassets.vanilla_status("1.21.1") == "ready"
    with zipfile.ZipFile(itemassets._vanilla_zip("1.21.1")) as zf:
        names = set(zf.namelist())
    assert "net/minecraft/Main.class" not in names
    assert "assets/minecraft/sounds.json" not in names
    assert "assets/minecraft/lang/de_de.json" in names
    index = itemassets.ItemIndex([itemassets._vanilla_zip("1.21.1")])
    assert index.names["minecraft:stone"] == "Stein"
    assert index.icon("minecraft:stone").endswith(b"stone")
