"""Minimaler RCON-Client (Source-RCON-Protokoll) ohne externe Abhängigkeiten.

Paketformat (Little-Endian):
    [Laenge:int32][Request-ID:int32][Typ:int32][Payload][0x00][0x00]
Typen: 3 = Login, 2 = Kommando/Antwort. Auth-Fehlschlag → Request-ID -1.
"""
import re
import socket
import struct

_LIST_RE = re.compile(
    r"There are (\d+) of a max of (\d+) players online(?::\s*(.*))?", re.S)


class RconError(RuntimeError):
    pass


def _send(sock: socket.socket, req_id: int, ptype: int, payload: bytes) -> None:
    data = struct.pack("<ii", req_id, ptype) + payload + b"\x00\x00"
    sock.sendall(struct.pack("<i", len(data)) + data)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(min(65536, size - len(chunks)))
        if not chunk:
            raise RconError("Verbindung vorzeitig geschlossen")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_packet(sock: socket.socket) -> tuple:
    (length,) = struct.unpack("<i", _recv_exact(sock, 4))
    if not 10 <= length <= 65535:
        raise RconError(f"Ungültige Paketgröße: {length}")
    data = _recv_exact(sock, length)
    req_id, ptype = struct.unpack("<ii", data[:8])
    return req_id, ptype, data[8:-2]


def command(host: str, port: int, password: str, cmd: str,
            timeout: float = 5.0) -> str:
    """Loggt sich ein, führt ein Kommando aus und gibt die Antwort zurück.
    Wirft RconError bei Verbindungs-, Auth- oder Protokollproblemen."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            sock.settimeout(timeout)
            _send(sock, 1, 3, password.encode("utf-8"))
            req_id, _ptype, _payload = _read_packet(sock)
            if req_id == -1:
                raise RconError("RCON-Login fehlgeschlagen (Passwort falsch?)")
            _send(sock, 2, 2, cmd.encode("utf-8"))
            req_id, _ptype, payload = _read_packet(sock)
            if req_id == -1:
                raise RconError("RCON-Kommando abgelehnt")
            out = payload.decode("utf-8", "replace")
            # Mehrere Antwort-Pakete abholen, bis der Server nichts mehr sendet
            sock.settimeout(0.4)
            try:
                while True:
                    _rid, _ptype, more = _read_packet(sock)
                    if not more:
                        break
                    out += more.decode("utf-8", "replace")
            except (OSError, RconError):
                pass  # normales Ende der Antwort
            return out.strip()
    except RconError:
        raise
    except (OSError, ValueError) as exc:
        raise RconError(str(exc) or exc.__class__.__name__) from exc


def parse_list_output(text: str) -> dict | None:
    """Parst die Ausgabe von 'list': Spielerzahl, Slots und Namen."""
    match = _LIST_RE.search(text or "")
    if not match:
        return None
    online, maximum = int(match.group(1)), int(match.group(2))
    raw_names = match.group(3) or ""
    names = [n.strip() for n in raw_names.split(",") if n.strip()]
    return {"online": online, "max": maximum, "names": names}


__all__ = ["RconError", "command", "parse_list_output"]
