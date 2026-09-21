"""Tests für app.minecraft: VarInt, MOTD-Flattening, SLP (Offline + Mock-Server)."""
import json
import socket
import threading

from app.minecraft import (
    _encode_varint,
    _flatten_motd,
    _send_packet,
    server_status,
)


class TestVarInt:
    def test_kleine_werte(self):
        assert _encode_varint(0) == b"\x00"
        assert _encode_varint(1) == b"\x01"
        assert _encode_varint(127) == b"\x7f"

    def test_mehrbyte(self):
        assert _encode_varint(128) == b"\x80\x01"
        assert _encode_varint(300) == b"\xac\x02"

    def test_negativ_32bit(self):
        # -1 als 32-bit-Unsigned (Protokoll-Placeholder) -> 5 Bytes
        assert _encode_varint(-1) == b"\xff\xff\xff\xff\x0f"


class TestFlattenMotd:
    def test_einfacher_string(self):
        assert _flatten_motd("Hallo") == "Hallo"

    def test_verschachtelte_komponente(self):
        motd = {"text": "A", "extra": [{"text": "B"}, {"text": "C", "extra": [{"text": "D"}]}]}
        assert _flatten_motd(motd) == "ABCD"

    def test_unbekannter_typ(self):
        assert _flatten_motd(12345) == ""


class _MockMCServer:
    """Minimaler TCP-Server, der auf einen SLP-Handshake ein Status-JSON antwortet."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(2.0)
            try:
                # Handshake + Status-Request lesen (bis Client still ist)
                while True:
                    if not conn.recv(4096):
                        break
            except (TimeoutError, OSError):
                pass
            data = json.dumps(self.payload).encode("utf-8")
            _send_packet(conn, b"\x00" + _encode_varint(len(data)) + data)

    def close(self):
        self.sock.close()


class TestServerStatus:
    def test_offline_geschlossener_port(self):
        result = server_status("127.0.0.1", 59999, timeout=1.0)
        assert result["online"] is False
        assert result["error"]  # Fehlertext vorhanden
        assert result["players"]["online"] == 0

    def test_online_mock_server(self):
        payload = {
            "version": {"name": "1.21.4"},
            "description": {"text": "Hallo ", "extra": [{"text": "Welt"}]},
            "players": {
                "online": 2,
                "max": 20,
                "sample": [{"name": "Steve", "uuid": "u-1"}, {"name": "Alex", "uuid": "u-2"}],
            },
        }
        server = _MockMCServer(payload)
        try:
            result = server_status("127.0.0.1", server.port, timeout=3.0)
        finally:
            server.close()
        assert result["online"] is True
        assert result["version"] == "1.21.4"
        assert result["motd"] == "Hallo Welt"
        assert result["error"] is None
        assert result["players"]["online"] == 2
        assert result["players"]["max"] == 20
        assert [p["name"] for p in result["players"]["sample"]] == ["Steve", "Alex"]

    def test_leeres_status_json(self):
        # Leeres Status-JSON ist gültig -> online mit Defaults
        server = _MockMCServer({})
        try:
            result = server_status("127.0.0.1", server.port, timeout=3.0)
        finally:
            server.close()
        assert result["online"] is True
        assert result["version"] is None
        assert result["players"]["sample"] == []


class TestProcessStats:
    def test_struktur(self):
        from app.minecraft import process_stats

        stats = process_stats()
        assert isinstance(stats, dict)
        assert "found" in stats
        if stats["found"]:
            assert 0 <= stats["cpu_percent"] <= 100
            assert stats["ram_mb"] >= 0
            assert stats["processes"] >= 1
        else:
            assert stats["reason"]
