"""Server-Status via Minecraft Server List Ping und Prozessstatistik via psutil.

Beides ohne schwergewichtige Abhängigkeiten: SLP nutzt nur die Standardbibliothek,
psutil ist die einzige externe Abhängigkeit (im Docker-Image enthalten).
"""
import json
import socket
import struct

_SLP_TIMEOUT = 3.0


def _encode_varint(value: int) -> bytes:
    value &= 0xFFFFFFFF  # VarInt ist 32-bit; -1 (Protokoll-Placeholder) wird korrekt kodiert
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(sock: socket.socket) -> int:
    number = 0
    for i in range(5):
        data = sock.recv(1)
        if not data:
            raise ValueError("Verbindung geschlossen")
        byte = data[0]
        number |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return number
    raise ValueError("Ungültiger VarInt")


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(min(65536, size - len(chunks)))
        if not chunk:
            raise ValueError("Verbindung vorzeitig geschlossen")
        chunks.extend(chunk)
    return bytes(chunks)


def _send_packet(sock: socket.socket, payload: bytes) -> None:
    sock.sendall(_encode_varint(len(payload)) + payload)


def _flatten_motd(description) -> str:
    """Minecraft-MOTDs sind str oder verschachtelte JSON-Textkomponenten."""
    if isinstance(description, str):
        return description
    if isinstance(description, dict):
        parts = [str(description.get("text") or "")]
        for extra in description.get("extra") or []:
            parts.append(_flatten_motd(extra))
        return "".join(parts)
    return ""


def server_status(host: str, port: int, timeout: float = _SLP_TIMEOUT) -> dict:
    """Fragt den Server per Server List Ping ab. Gibt bei jedem Fehler ein
    sauberes 'offline'-Objekt zurück (nie eine Exception)."""
    offline = {
        "online": False,
        "version": None,
        "motd": "",
        "players": {"online": 0, "max": 0, "sample": []},
        "error": None,
    }
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            host_bytes = host.encode("utf-8")
            handshake = (
                b"\x00"
                + _encode_varint(-1)  # Protokollversion unbekannt -> Status
                + _encode_varint(len(host_bytes))
                + host_bytes
                + struct.pack(">H", port)
                + _encode_varint(1)  # next_state: status
            )
            _send_packet(sock, handshake)
            _send_packet(sock, b"\x00")  # Status-Request (leer)
            _read_varint(sock)  # Paketgesamtlänge (ignorieren)
            packet_id = _read_varint(sock)
            if packet_id != 0:
                raise ValueError("Unerwartetes Status-Paket")
            length = _read_varint(sock)
            raw = _read_exact(sock, length)
            info = json.loads(raw.decode("utf-8", "replace"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        offline["error"] = str(exc) or exc.__class__.__name__
        return offline

    players = info.get("players") or {}
    sample = [
        {"name": str(p.get("name") or "?"), "uuid": str(p.get("uuid") or "")}
        for p in (players.get("sample") or [])
        if isinstance(p, dict)
    ]
    return {
        "online": True,
        "version": (info.get("version") or {}).get("name"),
        "motd": _flatten_motd(info.get("description"))[:200],
        "players": {
            "online": int(players.get("online") or 0),
            "max": int(players.get("max") or 0),
            "sample": sample,
        },
        "error": None,
    }


def process_stats() -> dict:
    """CPU/RAM aller sichtbaren Java-Prozesse. Voraussetzung: Dashboard-Container
    teilt den PID-Namespace mit dem Minecraft-Container (pid: service:minecraft)."""
    try:
        import psutil
    except ImportError:
        return {"found": False, "reason": "psutil nicht installiert",
                "cpu_percent": 0.0, "ram_mb": 0.0, "processes": 0}

    java_procs = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info.get("name") or "").lower()
        if name.startswith("java"):
            java_procs.append(proc)
    if not java_procs:
        return {"found": False, "reason": "Kein Java-Prozess sichtbar (PID-Namespace?)",
                "cpu_percent": 0.0, "ram_mb": 0.0, "processes": 0}

    cpu_total = 0.0
    rss_total = 0
    for proc in java_procs:
        try:
            cpu_total += proc.cpu_percent(None)  # seit letztem Aufruf
            rss_total += proc.memory_info().rss
        except Exception:
            continue
    cores = psutil.cpu_count(logical=True) or 1
    return {
        "found": True,
        "cpu_percent": round(min(cpu_total / cores, 100.0), 1),
        "ram_mb": round(rss_total / (1024 * 1024), 1),
        "processes": len(java_procs),
    }
