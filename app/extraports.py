"""Zusätzliche Ports je Instanz (z. B. Simple Voice Chat: UDP 24454).

Der Container veröffentlicht neben dem Spiel-Port nur, was hier eingetragen
ist. Host- und Container-Port sind identisch (Mods melden ihren Port an die
Clients, ein abweichender Host-Port würde dort nicht passen). Wirksam beim
nächsten (Neu-)Start; in der Fritzbox muss der Port ebenfalls freigegeben werden (siehe fritzbox.py).
"""
from fastapi import HTTPException

from . import instances

MAX_PORTS = 5
PROTOS = ("tcp", "udp")


def get_ports(instance: dict) -> list:
    raw = instance.get("extra_ports")
    return [p for p in raw if isinstance(p, dict)] if isinstance(raw, list) else []


def _validate(ports) -> list:
    if not isinstance(ports, list) or len(ports) > MAX_PORTS:
        raise HTTPException(status_code=400,
                            detail=f"Höchstens {MAX_PORTS} zusätzliche Ports")
    out, seen = [], set()
    for item in ports:
        try:
            port = int(item.get("port"))
        except (AttributeError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Port muss eine Zahl sein")
        proto = str(item.get("proto") or "").lower()
        if proto not in PROTOS:
            raise HTTPException(status_code=400, detail="Protokoll muss tcp oder udp sein")
        if not 1024 <= port <= 65535:
            raise HTTPException(status_code=400, detail="Port muss zwischen 1024 und 65535 liegen")
        if (port, proto) in seen:
            raise HTTPException(status_code=400, detail=f"Port {port}/{proto} doppelt")
        seen.add((port, proto))
        label = str(item.get("label") or "").strip()[:32]
        out.append({"port": port, "proto": proto, "label": label})
    return out


def set_ports(instance_id: str, ports) -> list:
    new = _validate(ports)
    with instances._LOCK:
        instance = instances.get_instance(instance_id)
        others = [i for i in instances.list_instances() if i["id"] != instance_id]
        used_game, used_rcon = instances._used_port_sets(others)
        own = {int(instance["port"]), int(instance.get("rcon_port") or int(instance["port"]) + 1000)}
        try:
            own_map = int((instance.get("map") or {}).get("port") or 0)
        except (TypeError, ValueError):
            own_map = 0
        if own_map:
            own.add(own_map)
        for entry in new:
            if entry["port"] in used_game | used_rcon | own:
                raise HTTPException(
                    status_code=409,
                    detail=f"Port {entry['port']} wird schon von einer Instanz benutzt")
        instance["extra_ports"] = new
        instances.update_instance(instance)
    return new
