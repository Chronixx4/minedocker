"""Kleiner Leser für binäres NBT (playerdata/*.dat, level.dat).

Nur lesen, nur Java Edition (Big Endian), gzip oder unkomprimiert. Ergebnis
sind einfache Python-Werte: Compound → dict, List/Arrays → list, Zahlen →
int/float, Strings → str. Schutz gegen kaputte Dateien: Größenlimit,
Verschachtelungstiefe und Längenprüfungen; Fehler werden zu NbtError.
"""
from __future__ import annotations

import gzip
import io
import struct
import zlib
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024  # entpackt; Spielerdaten liegen weit darunter
_MAX_DEPTH = 64


class NbtError(ValueError):
    pass


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise NbtError("NBT endet unerwartet")
        chunk = self.data[self.pos:self.pos + size]
        self.pos += size
        return chunk

    def unpack(self, fmt: str):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def string(self) -> str:
        return self.take(self.unpack(">H")).decode("utf-8", "replace")

    def payload(self, tag: int, depth: int):
        if depth > _MAX_DEPTH:
            raise NbtError("NBT zu tief verschachtelt")
        if tag == 1:
            return self.unpack(">b")
        if tag == 2:
            return self.unpack(">h")
        if tag == 3:
            return self.unpack(">i")
        if tag == 4:
            return self.unpack(">q")
        if tag == 5:
            return self.unpack(">f")
        if tag == 6:
            return self.unpack(">d")
        if tag == 7:
            return list(self.take(self.unpack(">i")))
        if tag == 8:
            return self.string()
        if tag == 9:
            item_tag = self.unpack(">b")
            count = self.unpack(">i")
            if count > len(self.data):
                raise NbtError("NBT-Liste zu lang")
            return [self.payload(item_tag, depth + 1) for _ in range(max(count, 0))]
        if tag == 10:
            out: dict = {}
            while True:
                child = self.unpack(">b")
                if child == 0:
                    return out
                name = self.string()
                out[name] = self.payload(child, depth + 1)
        if tag == 11:
            count = self.unpack(">i")
            return list(struct.unpack(f">{count}i", self.take(4 * count)))
        if tag == 12:
            count = self.unpack(">i")
            return list(struct.unpack(f">{count}q", self.take(8 * count)))
        raise NbtError(f"Unbekannter NBT-Typ {tag}")


def loads(data: bytes) -> dict:
    """Bytes (gzip oder roh) → Wurzel-Compound."""
    if data[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
                data = gz.read(MAX_BYTES + 1)
        except (OSError, EOFError, zlib.error) as exc:
            raise NbtError(f"gzip defekt: {exc}") from exc
    if len(data) > MAX_BYTES:
        raise NbtError("NBT-Datei zu groß")
    reader = _Reader(data)
    if reader.unpack(">b") != 10:
        raise NbtError("Wurzel ist kein Compound")
    reader.string()
    root = reader.payload(10, 0)
    return root


def load(path: Path) -> dict:
    with open(path, "rb") as fh:
        raw = fh.read(MAX_BYTES + 1)
    return loads(raw)


def dumps(root: dict) -> bytes:
    """Minimaler Schreiber (für Tests): dict/list/int/float/str → NBT, gzip."""
    def enc(value) -> tuple:
        if isinstance(value, bool):
            return 1, struct.pack(">b", int(value))
        if isinstance(value, int):
            return 3, struct.pack(">i", value)
        if isinstance(value, float):
            return 6, struct.pack(">d", value)
        if isinstance(value, str):
            raw = value.encode("utf-8")
            return 8, struct.pack(">H", len(raw)) + raw
        if isinstance(value, list):
            parts = [enc(v) for v in value]
            item_tag = parts[0][0] if parts else 0
            return 9, struct.pack(">bi", item_tag, len(parts)) + b"".join(p for _, p in parts)
        if isinstance(value, dict):
            body = b""
            for key, child in value.items():
                tag, payload = enc(child)
                name = key.encode("utf-8")
                body += struct.pack(">bH", tag, len(name)) + name + payload
            return 10, body + b"\x00"
        raise TypeError(type(value))
    _, payload = enc(root)
    return gzip.compress(b"\x0a\x00\x00" + payload)
