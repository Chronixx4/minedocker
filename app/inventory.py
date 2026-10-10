"""Spieler-Inventar über RCON: lesen (data get entity) und pro Slot ändern
(item replace / give).

Minecraft gibt Spielerdaten als SNBT-Text aus. Der kleine Parser hier hält
Zahlen samt Typ-Suffix (3b, 1.5f …) als Rohtext, damit ein Item beim
Verschieben exakt so zurückgeschrieben wird, wie der Server es geliefert hat.

Unterstützte Formate:
- bis 1.20.4: {Slot:0b, id:"…", Count:1b, tag:{…}}; Rüstung/Offhand als
  Slot 100-103 / -106 im Inventory
- ab 1.20.5: {Slot:0b, id:"…", count:1, components:{…}}
- ab 1.21.5: Rüstung/Offhand im eigenen Feld equipment:{head:…, offhand:…}

Direktes Schreiben in Spielerdaten (data modify) lässt Minecraft nicht zu,
deshalb arbeitet jede Änderung pro Slot über 'item replace' (ab 1.17).
"""
from __future__ import annotations

import hashlib
import json
import re

# ---------------------------------------------------------------------------
# SNBT
# ---------------------------------------------------------------------------


class SnbtError(ValueError):
    pass


class Tag:
    """Zahl oder Boolean im SNBT — Rohtext inkl. Suffix bleibt erhalten."""

    __slots__ = ("raw",)

    def __init__(self, raw: str) -> None:
        self.raw = raw

    def number(self) -> float | int | None:
        text = self.raw.lower()
        if text in ("true", "false"):
            return int(text == "true")
        if text[-1:] in ("b", "s", "l", "f", "d"):
            text = text[:-1]
        try:
            return int(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return None

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Tag) and other.raw == self.raw

    def __hash__(self) -> int:
        return hash(self.raw)

    def __repr__(self) -> str:
        return f"Tag({self.raw!r})"


class TypedArray(list):
    """[B;…], [I;…], [L;…]"""

    def __init__(self, kind: str, items) -> None:
        super().__init__(items)
        self.kind = kind


_NUMBER_RE = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?[bslfd]?$", re.I)
_UNQUOTED_RE = re.compile(r"[A-Za-z0-9._+\-:]")
_KEY_CHAR_RE = re.compile(r"[A-Za-z0-9._+\-]")
_SIMPLE_KEY_RE = re.compile(r"^[A-Za-z0-9._+\-]+$")


class _Parser:
    def __init__(self, text: str) -> None:
        self.s = text
        self.i = 0

    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    def peek(self) -> str:
        self.ws()
        return self.s[self.i] if self.i < len(self.s) else ""

    def expect(self, char: str) -> None:
        if self.peek() != char:
            raise SnbtError(f"'{char}' erwartet an Position {self.i}")
        self.i += 1

    def value(self):
        char = self.peek()
        if char == "{":
            return self.compound()
        if char == "[":
            return self.list()
        if char in "\"'":
            return self.quoted()
        token = self.unquoted()
        if not token:
            raise SnbtError(f"Wert erwartet an Position {self.i}")
        if _NUMBER_RE.match(token) or token in ("true", "false"):
            return Tag(token)
        return token

    def quoted(self) -> str:
        quote = self.s[self.i]
        self.i += 1
        out = []
        while self.i < len(self.s):
            char = self.s[self.i]
            if char == "\\" and self.i + 1 < len(self.s):
                out.append(self.s[self.i + 1])
                self.i += 2
                continue
            if char == quote:
                self.i += 1
                return "".join(out)
            out.append(char)
            self.i += 1
        raise SnbtError("String nicht beendet")

    def unquoted(self, pattern: re.Pattern = _UNQUOTED_RE) -> str:
        self.ws()
        start = self.i
        while self.i < len(self.s) and pattern.match(self.s[self.i]):
            self.i += 1
        return self.s[start:self.i]

    def key(self) -> str:
        if self.peek() in "\"'":
            return self.quoted()
        key = self.unquoted(_KEY_CHAR_RE)
        if not key:
            raise SnbtError(f"Schlüssel erwartet an Position {self.i}")
        return key

    def compound(self) -> dict:
        self.expect("{")
        out: dict = {}
        if self.peek() == "}":
            self.i += 1
            return out
        while True:
            key = self.key()
            self.expect(":")
            out[key] = self.value()
            if self.peek() == ",":
                self.i += 1
                continue
            self.expect("}")
            return out

    def list(self):
        self.expect("[")
        rest = self.s[self.i:self.i + 2]
        if len(rest) == 2 and rest[0] in "BIL" and rest[1] == ";":
            kind = rest[0]
            self.i += 2
            items = []
            if self.peek() != "]":
                while True:
                    items.append(self.value())
                    if self.peek() == ",":
                        self.i += 1
                        continue
                    break
            self.expect("]")
            return TypedArray(kind, items)
        items = []
        if self.peek() == "]":
            self.i += 1
            return items
        while True:
            items.append(self.value())
            if self.peek() == ",":
                self.i += 1
                continue
            self.expect("]")
            return items


def parse_snbt(text: str):
    parser = _Parser(text)
    value = parser.value()
    parser.ws()
    if parser.i != len(parser.s):
        raise SnbtError(f"Unerwarteter Rest an Position {parser.i}")
    return value


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_snbt(value) -> str:
    if isinstance(value, Tag):
        return value.raw
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, dict):
        parts = []
        for key, val in value.items():
            k = key if _SIMPLE_KEY_RE.match(key) else _quote(key)
            parts.append(f"{k}:{to_snbt(val)}")
        return "{" + ",".join(parts) + "}"
    if isinstance(value, TypedArray):
        return f"[{value.kind};" + ",".join(to_snbt(v) for v in value) + "]"
    if isinstance(value, list):
        return "[" + ",".join(to_snbt(v) for v in value) + "]"
    raise SnbtError(f"Unbekannter Typ: {type(value).__name__}")


def to_plain(value):
    """SNBT-Baum → JSON-taugliche Werte (für Anzeige)."""
    if isinstance(value, Tag):
        num = value.number()
        return value.raw if num is None else num
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_plain(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Antworten von 'data get entity'
# ---------------------------------------------------------------------------

PLAYER_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
ITEM_ID_RE = re.compile(r"^[a-z0-9_.\-]+:[a-z0-9_./\-]+$")


class PlayerOffline(Exception):
    pass


def parse_data_get(output: str):
    """Wert aus '<Name> has the following entity data: <SNBT>' holen.
    None, wenn das Feld nicht existiert; PlayerOffline, wenn der Spieler
    nicht gefunden wurde."""
    text = (output or "").strip()
    if "No entity was found" in text or "No player was found" in text:
        raise PlayerOffline()
    marker = "entity data: "
    pos = text.find(marker)
    if pos < 0:
        return None
    try:
        return parse_snbt(text[pos + len(marker):])
    except SnbtError:
        return None


# Slot-Nummern im alten Inventory-Format (bis 1.21.4)
_LEGACY_SPECIAL = {100: "armor.feet", 101: "armor.legs", 102: "armor.chest",
                   103: "armor.head", -106: "weapon.offhand"}
_EQUIPMENT = {"head": "armor.head", "chest": "armor.chest", "legs": "armor.legs",
              "feet": "armor.feet", "offhand": "weapon.offhand"}
ARMOR_SLOTS = ("armor.head", "armor.chest", "armor.legs", "armor.feet")


def _num(value, default=0):
    if isinstance(value, Tag):
        num = value.number()
        return default if num is None else num
    return default


def _is_item(value) -> bool:
    return (isinstance(value, dict) and isinstance(value.get("id"), str)
            and ":" in value["id"] and ("count" in value or "Count" in value))


def item_command_string(item: dict) -> str:
    """Item so formatiert, wie 'item replace … with <item>' es erwartet
    (ohne Menge)."""
    item_id = item["id"]
    if "components" in item or "count" in item:
        comps = item.get("components") or {}
        if not comps:
            return item_id
        parts = []
        for key, val in comps.items():
            if key.startswith("!"):
                parts.append(key)
            else:
                parts.append(f"{key}={to_snbt(val)}")
        return f"{item_id}[{','.join(parts)}]"
    tag = item.get("tag")
    return f"{item_id}{to_snbt(tag)}" if tag else item_id


def item_count(item: dict) -> int:
    return int(_num(item.get("count", item.get("Count")), 1))


def signature(item: dict | None) -> str:
    """Kurzer Fingerabdruck eines Slots (leer = "")."""
    if not item:
        return ""
    raw = f"{item_command_string(item)}|{item_count(item)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _text_component(value) -> str | None:
    """custom_name: JSON-String (bis 1.21.4) oder SNBT-Text (ab 1.21.5)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        raw = value.get("text")
        text: str = raw if isinstance(raw, str) else ""
        for extra in value.get("extra") or []:
            text += _text_component(extra) or ""
        return text or None
    if isinstance(value, list):
        return "".join(_text_component(v) or "" for v in value) or None
    return None


def _enchantments(item: dict) -> list:
    comps = item.get("components")
    if isinstance(comps, dict):
        found = []
        for key in ("minecraft:enchantments", "minecraft:stored_enchantments"):
            data = comps.get(key)
            if isinstance(data, dict):
                inner = data.get("levels")
                levels: dict = inner if isinstance(inner, dict) else data
                found += [{"id": k, "level": int(_num(v, 1))} for k, v in levels.items()
                          if k != "show_in_tooltip" and isinstance(v, Tag)]
        return found
    tag = item.get("tag")
    if isinstance(tag, dict):
        found = []
        for key in ("Enchantments", "StoredEnchantments"):
            for entry in tag.get(key) or []:
                if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                    found.append({"id": entry["id"], "level": int(_num(entry.get("lvl"), 1))})
        return found
    return []


def describe_item(item: dict, slot: str) -> dict:
    """Item für das Frontend aufbereiten."""
    raw_comps, raw_tag = item.get("components"), item.get("tag")
    comps: dict = raw_comps if isinstance(raw_comps, dict) else {}
    tag: dict = raw_tag if isinstance(raw_tag, dict) else {}
    damage = comps.get("minecraft:damage", tag.get("Damage"))
    max_damage = comps.get("minecraft:max_damage")
    custom_name = comps.get("minecraft:custom_name")
    if custom_name is None and isinstance(tag.get("display"), dict):
        custom_name = tag["display"].get("Name")
    data = item.get("components", item.get("tag"))
    command = item_command_string(item)
    return {
        "slot": slot,
        "id": item["id"],
        "count": item_count(item),
        "damage": int(_num(damage)) if isinstance(damage, Tag) else None,
        "max_damage": int(_num(max_damage)) if isinstance(max_damage, Tag) else None,
        "custom_name": _text_component(custom_name) if custom_name is not None else None,
        "enchantments": _enchantments(item),
        "data": to_snbt(data) if data else None,
        "sig": signature(item),
        # über RCON passen nur ~1400 Byte pro Befehl
        "movable": len(command.encode("utf-8")) <= MAX_ITEM_BYTES,
    }


def collect_slots(inventory, equipment, ender) -> dict:
    """Rohdaten → {slot_name: item_dict} (SNBT-Bäume)."""
    slots: dict = {}
    for entry in inventory or []:
        if not _is_item(entry):
            continue
        num = int(_num(entry.get("Slot"), -1))
        if 0 <= num <= 35:
            slots[f"container.{num}"] = entry
        elif num in _LEGACY_SPECIAL:
            slots[_LEGACY_SPECIAL[num]] = entry
    if isinstance(equipment, dict):
        for key, name in _EQUIPMENT.items():
            if _is_item(equipment.get(key)):
                slots[name] = equipment[key]
    for entry in ender or []:
        if not _is_item(entry):
            continue
        num = int(_num(entry.get("Slot"), -1))
        if 0 <= num <= 26:
            slots[f"enderchest.{num}"] = entry
    return slots


def find_mod_items(tree, path: str = "", out: list | None = None, depth: int = 0) -> list:
    """Items in Mod-Daten (Curios, Trinkets, Accessories …) finden — nur zum
    Ansehen. Gruppe = erster sprechender Schlüssel auf dem Weg dorthin."""
    if out is None:
        out = []
    if depth > 12 or len(out) >= 200:
        return out
    if _is_item(tree):
        out.append((path, tree))
        return out
    if isinstance(tree, dict):
        # Curios: [{Identifier:"ring", StacksHandler:{…}}] → Gruppe "ring"
        label = tree.get("Identifier", tree.get("identifier"))
        if isinstance(label, str) and label:
            path = f"{path}/{label}" if path else label
        for key, val in tree.items():
            find_mod_items(val, f"{path}/{key}" if path else key, out, depth + 1)
    elif isinstance(tree, list):
        for val in tree:
            find_mod_items(val, path, out, depth + 1)
    return out


_GROUP_SKIP = {"items", "stacks", "stackshandler", "cosmetics", "inventory", "curios",
               "slots", "contents"}


def _group_name(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    for part in reversed(parts):
        bare = part.split(":")[-1]
        if bare.lower() not in _GROUP_SKIP:
            return bare
    return parts[0] if parts else "Mod"


# Felder für die Mod-Slot-Suche je Loader
MOD_DATA_FIELDS = {
    "neoforge": ["neoforge:attachments"],
    "forge": ["ForgeCaps", "neoforge:attachments"],
    "fabric": ["cardinal_components"],
    "quilt": ["cardinal_components"],
}

STAT_FIELDS = ("SelectedItemSlot", "Health", "foodLevel", "XpLevel", "Pos",
               "Dimension", "playerGameType")
GAMEMODES = {0: "survival", 1: "creative", 2: "adventure", 3: "spectator"}

MAX_ITEM_BYTES = 1400


def read_commands(player: str, loader: str) -> list:
    base = [f"data get entity {player} {field}"
            for field in ("Inventory", "EnderItems", "equipment", *STAT_FIELDS)]
    # Schlüssel mit Doppelpunkt müssen im NBT-Pfad in Anführungszeichen stehen
    base += [f'data get entity {player} "{field}"' if ":" in field
             else f"data get entity {player} {field}"
             for field in MOD_DATA_FIELDS.get(loader, [])]
    return base


def build_snapshot(player: str, loader: str, outputs: list) -> tuple:
    """RCON-Antworten (Reihenfolge wie read_commands) → (Antwort fürs
    Frontend, {slot: SNBT-Item})."""
    values = [parse_data_get(out) for out in outputs]
    inventory, ender, equipment = values[0], values[1], values[2]
    if inventory is None:
        # Ohne Inventory-Feld ist das kein Spieler (oder der Server antwortet anders)
        raise PlayerOffline()
    stats_raw = dict(zip(STAT_FIELDS, values[3:3 + len(STAT_FIELDS)], strict=True))
    slots = collect_slots(inventory, equipment, ender)
    mod_items = []
    for tree in values[3 + len(STAT_FIELDS):]:
        for path, item in find_mod_items(tree):
            mod_items.append({**describe_item(item, ""), "group": _group_name(path),
                              "movable": False})
    pos = stats_raw.get("Pos")
    gamemode = stats_raw.get("playerGameType")
    snapshot = {
        "player": player,
        "selected_slot": int(_num(stats_raw.get("SelectedItemSlot"))),
        "stats": {
            "health": round(float(_num(stats_raw.get("Health"))), 1),
            "food": int(_num(stats_raw.get("foodLevel"))),
            "level": int(_num(stats_raw.get("XpLevel"))),
            "pos": [round(float(_num(p))) for p in pos] if isinstance(pos, list) else None,
            "dimension": stats_raw.get("Dimension")
            if isinstance(stats_raw.get("Dimension"), str) else None,
            "gamemode": GAMEMODES.get(int(_num(gamemode, -1))),
        },
        "slots": {name: describe_item(item, name) for name, item in slots.items()},
        "mod_items": mod_items,
    }
    return snapshot, slots


# ---------------------------------------------------------------------------
# Schreiben
# ---------------------------------------------------------------------------

SLOT_RE = re.compile(r"^(container\.(\d|[12]\d|3[0-5])|enderchest\.(\d|1\d|2[0-6])"
                     r"|armor\.(head|chest|legs|feet)|weapon\.offhand)$")


def version_tuple(version: str) -> tuple:
    nums = re.findall(r"\d+", (version or "").split("-")[0])
    return tuple(int(n) for n in nums[:3]) or (0,)


def editing_supported(game_version: str) -> bool:
    """'item replace' gibt es ab 1.17. Snapshots (z. B. 24w14a) gelten als neu."""
    if re.match(r"^\d{2}w\d{2}", game_version or ""):
        return True
    return version_tuple(game_version) >= (1, 17)


def replace_command(player: str, slot: str, item: dict | None, count: int | None = None) -> str:
    if item is None:
        return f"item replace entity {player} {slot} with minecraft:air"
    amount = item_count(item) if count is None else count
    cmd = f"item replace entity {player} {slot} with {item_command_string(item)} {amount}"
    if len(cmd.encode("utf-8")) > MAX_ITEM_BYTES + 100:
        raise ValueError("Das Item hat zu viele Daten, um es über RCON zu schreiben")
    return cmd


def give_command(player: str, item_id: str, count: int) -> str:
    return f"give {player} {item_id} {count}"


def write_ok(output: str) -> bool:
    """Server-Antworten sind immer Englisch (en_us)."""
    text = output or ""
    return text.startswith("Replaced") or text.startswith("Gave")


def plan_action(player: str, action: str, slots: dict, slot: str | None,
                to: str | None, count: int | None, item_id: str | None) -> list:
    """Befehle für eine Aktion aus dem AKTUELLEN Serverstand bauen. Das
    Frontend liefert nie Item-Daten — nur Slots, Menge und (bei give) die ID."""
    if action == "give":
        return [give_command(player, item_id or "", count or 1)]
    source = slots.get(slot or "")
    if action == "clear":
        return [replace_command(player, slot or "", None)]
    if action == "set_count":
        if source is None:
            raise ValueError("Der Slot ist leer")
        return [replace_command(player, slot or "", source, count)]
    if action == "move":
        if source is None:
            raise ValueError("Der Slot ist leer")
        target = slots.get(to or "")
        # Ziel zuerst schreiben (Quelle bleibt bis dahin erhalten), dann
        # Quelle mit dem alten Ziel-Item (Tausch) oder leer.
        return [replace_command(player, to or "", source),
                replace_command(player, slot or "", target)]
    raise ValueError(f"Unbekannte Aktion: {action}")


__all__ = [
    "ARMOR_SLOTS", "ITEM_ID_RE", "PLAYER_RE", "SLOT_RE", "PlayerOffline", "SnbtError",
    "Tag", "build_snapshot", "describe_item", "editing_supported", "item_command_string",
    "parse_data_get", "parse_snbt", "plan_action", "read_commands", "signature",
    "to_snbt", "write_ok",
]
