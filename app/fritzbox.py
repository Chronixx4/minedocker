"""Fritzbox-Anbindung (TR-064): Portfreigaben direkt aus dem Dashboard.

Die Zugangsdaten (Fritzbox-Adresse, Benutzer, Passwort, LAN-IP des Hosts)
werden im Dashboard eingetragen und in /data/fritzbox.json (Modus 0600)
gespeichert — das Passwort verlässt das Backend nie wieder (die API meldet
nur „hinterlegt ja/nein"). Die Verbindung läuft per HTTP (Port 49000) im LAN
mit Digest-Authentifizierung; dafür braucht die Fritzbox einen Benutzer mit
dem Recht „FRITZ!Box Einstellungen" und aktivierten Zugriff für Anwendungen
(Heimnetz → Netzwerk → Netzwerkeinstellungen → Weitere Einstellungen).

Freigaben des Dashboards tragen die Beschreibung „minedocker: <Name>";
fremde Freigaben der Fritzbox werden nie angefasst.
"""
import ipaddress
import json
import logging
import os
import re
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
from fastapi import HTTPException

from . import auth, livemap

logger = logging.getLogger("dashboard.fritzbox")

PREFIX = "minedocker"
PORT = 49000
# (Steuer-URL, Service-Typ): Kabel-/DSL-Boxen melden WANIPConnection oder
# WANPPPConnection — die erste, die antwortet, wird benutzt.
_SERVICES = (
    ("/upnp/control/wanipconnection1", "urn:dslforum-org:service:WANIPConnection:1"),
    ("/upnp/control/wanpppconnection1", "urn:dslforum-org:service:WANPPPConnection:1"),
)
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_LOCK = threading.Lock()

DEFAULTS = {"host": "fritz.box", "user": "", "password": "",
            "lan_ip": "", "auto": False}


def config_path() -> Path:
    return auth.users_path().parent / "fritzbox.json"


def load() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    out = dict(DEFAULTS)
    if isinstance(data, dict):
        for key in DEFAULTS:
            if key in data:
                out[key] = data[key]
    out["auto"] = bool(out["auto"])
    return out


def public_view(cfg: dict | None = None) -> dict:
    """Konfiguration ohne Passwort (für das Frontend)."""
    cfg = cfg or load()
    return {"host": cfg["host"], "user": cfg["user"], "lan_ip": cfg["lan_ip"],
            "auto": cfg["auto"], "has_password": bool(cfg["password"])}


def configured(cfg: dict | None = None) -> bool:
    cfg = cfg or load()
    return bool(cfg["user"] and cfg["password"] and cfg["lan_ip"])


def save(host=None, user=None, password=None, lan_ip=None, auto=None) -> dict:
    """Teil-Update; password=None/"" lässt das Passwort unverändert."""
    with _LOCK:
        cfg = load()
        if host is not None:
            host = host.strip() or DEFAULTS["host"]
            if not _HOST_RE.match(host):
                raise HTTPException(status_code=400, detail="Ungültige Fritzbox-Adresse")
            cfg["host"] = host
        if user is not None:
            user = user.strip()
            if len(user) > 64 or any(c in user for c in "\r\n\x00"):
                raise HTTPException(status_code=400, detail="Ungültiger Benutzername")
            cfg["user"] = user
        if password:
            if len(password) > 128 or any(c in password for c in "\r\n\x00"):
                raise HTTPException(status_code=400, detail="Ungültiges Passwort")
            cfg["password"] = password
        if lan_ip is not None:
            lan_ip = lan_ip.strip()
            if lan_ip:
                try:
                    addr = ipaddress.ip_address(lan_ip)
                except ValueError:
                    raise HTTPException(status_code=400,
                                        detail="LAN-IP ist keine gültige IP-Adresse") from None
                if addr.version != 4 or not addr.is_private:
                    raise HTTPException(
                        status_code=400,
                        detail="LAN-IP muss eine private IPv4-Adresse sein (z. B. 192.168.178.20)")
            cfg["lan_ip"] = lan_ip
        if auto is not None:
            cfg["auto"] = bool(auto)
        path = config_path()
        tmp = path.with_suffix(".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(cfg, handle)
            os.replace(tmp, path)
        except OSError as exc:
            raise HTTPException(status_code=500,
                                detail=f"fritzbox.json nicht schreibbar: {exc}") from exc
    return public_view(cfg)


# ---------- SOAP-Client ----------

class FritzError(RuntimeError):
    pass


def _envelope(service: str, action: str, args: dict) -> str:
    body = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in args.items())
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body>'
            '</s:Envelope>')


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _call(cfg: dict, action: str, args: dict | None = None) -> dict:
    """SOAP-Aufruf; probiert IP- und PPP-Service. Wirft FritzError."""
    last = "Fritzbox nicht erreichbar"
    for path, service in _SERVICES:
        url = f"http://{cfg['host']}:{PORT}{path}"
        headers = {"Content-Type": 'text/xml; charset="utf-8"',
                   "SOAPACTION": f'"{service}#{action}"'}
        try:
            resp = httpx.post(url, content=_envelope(service, action, args or {}),
                              headers=headers, timeout=8.0,
                              auth=httpx.DigestAuth(cfg["user"], cfg["password"]))
        except httpx.HTTPError as exc:
            raise FritzError(f"Fritzbox nicht erreichbar ({cfg['host']}): {exc}") from exc
        if resp.status_code == 401:
            raise FritzError("Anmeldung an der Fritzbox fehlgeschlagen — Benutzer, "
                             "Passwort und Recht „FRITZ!Box Einstellungen“ prüfen")
        if resp.status_code == 404:
            last = "Dienst nicht gefunden"
            continue
        fields = _parse(resp.text)
        if resp.status_code >= 400:
            code = fields.get("errorCode", "")
            raise FritzError(f"Fritzbox-Fehler {code or resp.status_code}: "
                             f"{fields.get('errorDescription', 'unbekannt')}")
        return fields
    raise FritzError(last)


def _parse(text: str) -> dict:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    out = {}
    for el in root.iter():
        if len(el) == 0 and el.text is not None:
            out[_local(el.tag)] = el.text.strip()
    return out


def _cfg_ready() -> dict:
    cfg = load()
    if not configured(cfg):
        raise HTTPException(status_code=409,
                            detail="Fritzbox ist noch nicht eingerichtet "
                                   "(Benutzer, Passwort und LAN-IP eintragen)")
    return cfg


def _wrap(exc: FritzError) -> HTTPException:
    return HTTPException(status_code=502, detail=str(exc))


def test() -> dict:
    cfg = _cfg_ready()
    try:
        ip = _call(cfg, "GetExternalIPAddress").get("NewExternalIPAddress", "")
        mappings = list_mappings(cfg)
    except FritzError as exc:
        raise _wrap(exc) from exc
    return {"external_ip": ip, "mappings": len(mappings)}


def list_mappings(cfg: dict | None = None) -> list:
    cfg = cfg or _cfg_ready()
    out = []
    for index in range(0, 200):
        try:
            f = _call(cfg, "GetGenericPortMappingEntry", {"NewPortMappingIndex": index})
        except FritzError as exc:
            if "713" in str(exc):  # SpecifiedArrayIndexInvalid = Ende der Liste
                break
            raise
        desc = f.get("NewPortMappingDescription", "")
        out.append({"port": int(f.get("NewExternalPort") or 0),
                    "proto": f.get("NewProtocol", "").lower(),
                    "internal_port": int(f.get("NewInternalPort") or 0),
                    "client": f.get("NewInternalClient", ""),
                    "enabled": f.get("NewEnabled") == "1",
                    "description": desc,
                    "ours": desc.startswith(PREFIX)})
    return out


def list_mappings_api() -> list:
    try:
        return list_mappings()
    except FritzError as exc:
        raise _wrap(exc) from exc


def required_ports(instance: dict) -> list:
    """Ports, die für Spieler von außen erreichbar sein müssen."""
    name = instance.get("name") or instance["id"]
    ports = [{"port": int(instance["port"]), "proto": "tcp", "label": f"{name} Spiel"}]
    map_port = livemap.map_port(instance)
    if livemap.enabled(instance) and map_port and livemap.is_public(instance):
        ports.append({"port": map_port, "proto": "tcp", "label": f"{name} Karte"})
    for extra in instance.get("extra_ports") or []:
        try:
            ports.append({"port": int(extra["port"]), "proto": extra["proto"],
                          "label": f"{name} {extra.get('label') or 'Zusatz'}".strip()})
        except (KeyError, TypeError, ValueError):
            continue
    return ports


def instance_status(instance: dict) -> dict:
    wanted = required_ports(instance)
    cfg = load()
    result: dict = {"configured": configured(cfg), "ports": [], "error": None}
    open_set = {}
    if result["configured"]:
        try:
            open_set = {(m["port"], m["proto"]): m for m in list_mappings(cfg)}
        except FritzError as exc:
            result["error"] = str(exc)
    for item in wanted:
        entry = open_set.get((item["port"], item["proto"]))
        result["ports"].append({**item, "open": bool(entry and entry["enabled"]),
                                "client": entry["client"] if entry else None})
    return result


def open_ports(instance: dict) -> list:
    cfg = _cfg_ready()
    done = []
    try:
        for item in required_ports(instance):
            _call(cfg, "AddPortMapping", {
                "NewRemoteHost": "", "NewExternalPort": item["port"],
                "NewProtocol": item["proto"].upper(), "NewInternalPort": item["port"],
                "NewInternalClient": cfg["lan_ip"], "NewEnabled": 1,
                "NewPortMappingDescription": f"{PREFIX}: {item['label']}"[:60],
                "NewLeaseDuration": 0})
            done.append(item)
    except FritzError as exc:
        raise _wrap(exc) from exc
    logger.info("Fritzbox: %d Port(s) freigegeben für %s", len(done), instance["id"])
    return done


def close_ports(instance: dict) -> list:
    cfg = _cfg_ready()
    done = []
    try:
        existing = {(m["port"], m["proto"]): m for m in list_mappings(cfg)}
        for item in required_ports(instance):
            entry = existing.get((item["port"], item["proto"]))
            if not entry or not entry["ours"]:
                continue  # nichts da oder nicht vom Dashboard angelegt
            _call(cfg, "DeletePortMapping", {
                "NewRemoteHost": "", "NewExternalPort": item["port"],
                "NewProtocol": item["proto"].upper()})
            done.append(item)
    except FritzError as exc:
        raise _wrap(exc) from exc
    logger.info("Fritzbox: %d Freigabe(n) entfernt für %s", len(done), instance["id"])
    return done


def auto_sync(instance: dict, opening: bool) -> None:
    """Best effort beim Start/Stopp einer Instanz (nur wenn „Automatisch" an);
    Fehler stören den Server-Start nie."""
    cfg = load()
    if not (cfg["auto"] and configured(cfg)):
        return
    try:
        (open_ports if opening else close_ports)(instance)
    except Exception as exc:
        logger.warning("Fritzbox-Automatik fehlgeschlagen (%s): %s", instance.get("id"), exc)
