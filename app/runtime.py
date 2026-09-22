"""Container-Runtime: startet/stoppt einzelne Server-Instanzen über Docker.

Architektur:
- Pro Instanz läuft ein eigener Container (itzg/minecraft-server), der die
  passende Server-Software (Fabric/Forge/NeoForge/Quilt/Paper/Spigot/Bukkit)
  automatisch herunterlädt — gesteuert über Env-Variablen.
- Das Instanz-Verzeichnis wird als Bind-Mount in den Container gehängt.
  Der Host-Pfad wird automatisch aus /proc/self/mountinfo erkannt oder via
  INSTANCES_HOST_DIR gesetzt.
- Neue Container werden automatisch in das Docker-Netzwerk des
  Dashboard-Containers gehängt, damit der Dashboard SLP-Pings ausführen kann.
"""
import hashlib
import logging
import re
import secrets
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import settings

logger = logging.getLogger("dashboard.runtime")

IMAGE = "itzg/minecraft-server:latest"

# RCON-Port im Container (itzg-Standard); wird als port+1000 auf den Host
# gemappt, damit der Dashboard-Container auch außerhalb von Docker RCON nutzt
_RCON_PORT = 25575

# Loader → itzg/minecraft-server TYPE-Env
_ITYZG_TYPE = {
    "fabric": "FABRIC",
    "forge": "FORGE",
    "neoforge": "NEOFORGE",
    "quilt": "QUILT",
    "bukkit": "BUKKIT",
    "spigot": "SPIGOT",
    "paper": "PAPER",
}

_client = None  # gecacheter Docker-Client (Tests injizieren hier einen Fake)
_LOCK = threading.Lock()


def _get_client():
    """Lazier Docker-Client; docker-Paket ist nur im Container nötig."""
    global _client
    with _LOCK:
        if _client is None:
            try:
                import docker  # erst hier importieren, damit Tests ohne Paket laufen
            except ImportError as exc:
                raise RuntimeError(
                    "docker-Paket nicht installiert — bitte requirements installieren"
                ) from exc
            try:
                _client = docker.from_env()
            except Exception as exc:
                hint = ""
                if "permission denied" in str(exc).lower():
                    hint = (
                        " — Socket-Zugriff verweigert: DOCKER_GID (docker-compose.yml"
                        " bzw. .env) auf die Host-GID von /var/run/docker.sock setzen,"
                        " ermitteln mit: stat -c '%g' /var/run/docker.sock"
                    )
                raise RuntimeError(f"Docker nicht erreichbar: {exc}{hint}") from exc
        return _client


def _rcon_salt() -> str:
    """Geteilter Zufalls-Salt für alle Instanz-RCON-Passwörter. Liegt als
    Datei im Instanz-Ordner; Bestandsinstanzen funktionieren so ohne Migration."""
    path = Path(settings.instances_dir) / ".rcon_salt"
    try:
        salt = path.read_text(encoding="utf-8").strip()
        if salt:
            return salt
    except (FileNotFoundError, OSError):
        pass
    salt = secrets.token_hex(24)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(salt + "\n", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"RCON-Salt nicht schreibbar: {exc}") from exc
    return salt


def rcon_secret(instance: dict) -> str:
    """Deterministisches RCON-Passwort pro Instanz (SHA-256 aus Salt + ID)."""
    salt = _rcon_salt()
    return hashlib.sha256(f"{salt}:{instance.get('id', '')}".encode()).hexdigest()[:24]


def rcon_port(instance: dict) -> int:
    """Host-Port des RCON-Endpunkts; Fallback port+1000 für Bestandsinstanzen."""
    try:
        return int(instance.get("rcon_port") or (int(instance["port"]) + 1000))
    except (TypeError, ValueError):
        return int(instance["port"]) + 1000


def rcon_target(instance: dict) -> tuple:
    """(Host, Port) für RCON aus Sicht des Dashboards:
    im Docker-Netz direkt über den Container-Namen, sonst über den
    veröffentlichten Host-Port."""
    if Path("/.dockerenv").exists():
        return container_name(instance["id"]), _RCON_PORT
    return "127.0.0.1", rcon_port(instance)


def _env(instance: dict) -> list:
    """itzg/minecraft-server-Umgebung für diese Instanz."""
    loader = instance["loader"]
    env = [
        "EULA=TRUE",
        f"TYPE={_ITYZG_TYPE.get(loader, 'VANILLA')}",
        f"VERSION={instance['game_version']}",
        f"MEMORY={instance.get('memory') or settings.instances_memory}",
        f"MOTD={instance['name']} (verwaltet vom Dashboard)",
        # RCON für Spieler-Verwaltung (op/ban/kick) über das Dashboard
        "ENABLE_RCON=TRUE",
        f"RCON_PASSWORD={rcon_secret(instance)}",
        f"RCON_PORT={_RCON_PORT}",
    ]
    loader_version = instance.get("loader_version")
    if loader_version:
        if loader in ("fabric", "quilt"):
            env.append(f"LOADER_VERSION={loader_version}")
        elif loader == "forge":
            env.append(f"FORGE_VERSION={loader_version}")
        elif loader == "neoforge":
            env.append(f"NEOFORGE_VERSION={loader_version}")
        elif loader == "paper":
            env.append(f"PAPER_BUILD={loader_version}")
    # Pro Instanz konfigurierbare JVM-Optionen (wirken beim nächsten Start)
    jvm_opts = (instance.get("jvm_opts") or "").strip()
    if jvm_opts:
        env.append(f"JVM_OPTS={jvm_opts}")
    if instance.get("use_aikar"):
        env.append("USE_AIKAR_FLAGS=TRUE")
    return env


def _host_instances_root() -> str:
    """Host-Pfad des Instanz-Ordners für Docker-Binds ermitteln.

    1. EXPLIZIT: INSTANCES_HOST_DIR (wenn gesetzt).
    2. AUTORITATIV: Docker-Daemon fragen — die Mount-Source unseres eigenen
       Containers (/data) ist genau der Pfad, den der Daemon für Binds nutzt.
       Das ist die einzige verlässliche Quelle; mountinfo-Roots sind je nach
       Plattform (Docker Desktop/WSL2, ZimaOS) nicht daemon-kompatibel.
    3. FALLBACK: /proc/self/mountinfo (echte Linux-Hosts mit Bind-Mounts).
    """
    if settings.instances_host_dir:
        return settings.instances_host_dir
    inst = Path(settings.instances_dir)

    # 2) Daemon befragen (autoritativ für Container-Binds)
    try:
        client = _get_client()
        me = client.containers.get(socket.gethostname())
        for mount in (me.attrs or {}).get("Mounts") or []:
            dest = mount.get("Destination")
            source = mount.get("Source")
            if not dest or not source:
                continue
            try:
                rel = inst.relative_to(dest)
            except ValueError:
                continue
            return str(Path(source) / rel)
    except Exception:
        pass  # kein Docker/kein Container (lokale Entwicklung) → Fallback

    # 3) Fallback: mountinfo (längster passender Mountpoint + relativer Rest)
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(
            "Host-Pfad des Instanz-Ordners konnte nicht ermittelt werden — "
            f"/proc/self/mountinfo nicht lesbar ({exc}); bitte INSTANCES_HOST_DIR "
            "auf den Host-Pfad von INSTANCES_DIR setzen"
        ) from exc
    inst_path = str(inst).rstrip("/") or "/"
    best = None  # (mountpoint, root) mit dem längsten passenden Mountpoint
    for line in lines:
        parts = line.split()
        # Format: ID parent major:minor root mountpoint mountopts ... - fstype source ...
        if len(parts) < 5:
            continue
        mountpoint, root = parts[4], parts[3]
        if (mountpoint == inst_path or inst_path.startswith(mountpoint.rstrip("/") + "/")) \
                and (best is None or len(mountpoint) > len(best[0])):
            best = (mountpoint, root)
    if best is None:
        raise RuntimeError(
            "Host-Pfad des Instanz-Ordners konnte nicht ermittelt werden — "
            "bitte INSTANCES_HOST_DIR auf den Host-Pfad von INSTANCES_DIR setzen"
        )
    mountpoint, root = best
    rel_rest = inst_path[len(mountpoint):].lstrip("/")
    host = "" if root == "/" else root.rstrip("/")
    return f"{host}/{rel_rest}" if rel_rest else (host or "/")


def container_name(instance_id: str) -> str:
    return f"mc-inst-{instance_id}"


def _get_container(client, instance: dict):
    try:
        return client.containers.get(container_name(instance["id"]))
    except Exception as exc:
        cls = exc.__class__.__name__
        if "NotFound" in cls or "not found" in str(exc).lower():
            return None
        raise RuntimeError(f"Container-Status nicht abfragbar: {exc}") from exc


def _network_of_dashboard(client):
    """Docker-Netzwerk des Dashboard-Containers (für RCON-Namensauflösung und
    SLP-Pings an Instanzen).

    Hängt das Dashboard NUR am Default-Bridge (z. B. nach einem ZimaOS-App-
    Import, der das Compose-Netzwerk unterschlägt), löst Docker dort KEINE
    Container-Namen auf — RCON (Errno -2) und SLP wären kaputt. In dem Fall
    wird einmalig ein benanntes Netzwerk 'mc-dashboard-net' erstellt und das
    Dashboard verbunden; neue Instanzen landen automatisch dort.
    """
    try:
        me = client.containers.get(socket.gethostname())
        networks = (me.attrs.get("NetworkSettings") or {}).get("Networks") or {}
        names = [n for n in networks if n not in ("bridge", "host", "none")]
        if names:
            return names[0]
        # Bridge-only: benanntes Netzwerk sicherstellen und verbinden
        net = _ensure_network(client)
        if net is None:
            return None
        try:
            net.connect(me)
        except Exception:
            pass  # bereits verbunden
        return net.name
    except Exception:
        return None  # außerhalb eines Containers (lokale Entwicklung)


def _ensure_network(client):
    """Legt 'mc-dashboard-net' an (idempotent) oder liefert None bei Fehler."""
    try:
        return client.networks.get(_NET_NAME)
    except Exception:
        pass  # existiert noch nicht → unten anlegen
    try:
        return client.networks.create(_NET_NAME, driver="bridge")
    except Exception:
        return None  # z. B. eingeschränkte Socket-Rechte → alter Fallback


def _mem_limit(instance: dict) -> int:
    """Container-Memory-Limit: JVM-Heap (MEMORY, z. B. '4G') + 1 GB Headroom
    für JVM-Overhead (Metaspace, Thread-Stacks, GC, Direkt-Puffer).
    Verhindert, dass eine Instanz den Host verhungern kann (§1 SERVER-SETUP.md).
    0 = kein Limit (nur bei ungültiger Angabe)."""
    raw = instance.get("memory") or settings.instances_memory
    match = re.match(r"^(\d{1,4})([GgMm])$", raw or "")
    if not match:
        return 0
    value, unit = int(match.group(1)), match.group(2).lower()
    heap = value * (1024 ** 3) if unit == "g" else value * (1024 ** 2)
    return heap + 1024 ** 3


# Log-Rotation je Instanz-Container (verhindert unbegrenzte Logfiles)
_LOG_CONFIG = {
    "Type": "json-file",
    "Config": {"max-size": "10m", "max-file": "3"},
}

# Eigenes Netzwerk für Dashboard + Instanzen (benutzerdefinierte Netzwerke
# sind Voraussetzung für Docker-DNS/Namensauflösung → RCON, SLP)
_NET_NAME = "mc-dashboard-net"


def start_instance(instance: dict) -> None:
    """Startet (synchron) den Container einer Instanz; wirft RuntimeError bei Fehlern."""
    client = _get_client()
    name = container_name(instance["id"])
    existing = _get_container(client, instance)
    if existing is not None:
        state = ((existing.attrs or {}).get("State") or {})
        if state.get("Running"):
            raise RuntimeError("Instanz läuft bereits")
        existing.remove(force=True)  # gestoppten Rest entfernen und neu erstellen

    host_dir = Path(_host_instances_root()) / instance["id"]
    if not host_dir.name:
        raise RuntimeError("Instanz-Host-Pfad ergibt kein gültiges Verzeichnis")
    network = _network_of_dashboard(client)
    kwargs = {
        "image": IMAGE,
        "name": name,
        "detach": True,
        "environment": _env(instance),
        "ports": {"25565/tcp": int(instance["port"]),
                  "25575/tcp": rcon_port(instance)},
        "volumes": {str(host_dir): {"bind": "/data", "mode": "rw"}},
        "restart_policy": {"Name": "unless-stopped"},
        "labels": {"mc-dashboard.instance": instance["id"],
                   "mc-dashboard.managed": "true"},
        # Best Practices: Speicher-Deckel, Log-Rotation, Privilegien-Bremse
        "mem_limit": _mem_limit(instance) or None,
        "log_config": _LOG_CONFIG,
        "security_opt": ["no-new-privileges:true"],
    }
    if network:
        kwargs["network"] = network
    try:
        client.containers.run(**kwargs)
    except Exception as exc:
        raise RuntimeError(f"Container-Start fehlgeschlagen: {exc}") from exc
    logger.info("Instanz-Container gestartet: %s (Port %s)", name, instance["port"])


def stop_instance(instance: dict) -> None:
    """Stoppt den Container einer Instanz (falls vorhanden)."""
    client = _get_client()
    container = _get_container(client, instance)
    if container is None:
        return  # nichts zu tun
    try:
        container.stop(timeout=30)
    except Exception as exc:
        raise RuntimeError(f"Container-Stop fehlgeschlagen: {exc}") from exc
    logger.info("Instanz-Container gestoppt: %s", container_name(instance["id"]))


def remove_container(instance: dict) -> None:
    """Entfernt den Container einer Instanz zwangsweise (beim Löschen)."""
    client = _get_client()
    container = _get_container(client, instance)
    if container is not None:
        try:
            container.remove(force=True)
        except Exception as exc:
            raise RuntimeError(f"Container-Entfernen fehlgeschlagen: {exc}") from exc


def is_running(instance: dict) -> bool:
    """True, wenn der Instanz-Container aktuell läuft."""
    try:
        client = _get_client()
    except RuntimeError:
        return False
    container = _get_container(client, instance)
    if container is None:
        return False
    return bool(((container.attrs or {}).get("State") or {}).get("Running"))


def running_state(instance: dict) -> bool | None:
    """True/False = Container-Status; None = nicht prüfbar (Docker nicht
    erreichbar). Fail-closed-Variante von is_running für Aktionen, die den
    Status zwingend brauchen (z. B. Port-Wechsel)."""
    try:
        client = _get_client()
    except RuntimeError:
        return None
    container = _get_container(client, instance)
    if container is None:
        return False
    return bool(((container.attrs or {}).get("State") or {}).get("Running"))


def container_status(instance: dict) -> dict:
    """Status des Instanz-Containers: {'running': bool, 'container': str,
    'started_at': ISO-Zeitpunkt des letzten Starts (für die Uptime-Anzeige)}."""
    try:
        client = _get_client()
    except RuntimeError as exc:
        return {"running": False, "container": None, "started_at": None, "error": str(exc)}
    container = _get_container(client, instance)
    if container is None:
        return {"running": False, "container": None, "started_at": None, "error": None}
    state = (container.attrs or {}).get("State") or {}
    started_at = state.get("StartedAt") or None
    if started_at:
        # Nanosekunden kürzen (new Date() im Frontend parst nur Millisekunden)
        started_at = re.sub(r"(\.\d{3})\d+", r"\1", str(started_at))
    return {
        "running": bool(state.get("Running")),
        "container": state.get("Status") or "unknown",
        "started_at": started_at,
        "error": None,
    }


def logs(instance: dict, tail: int = 100) -> list:
    """Letzte Logzeilen des Instanz-Containers."""
    client = _get_client()
    container = _get_container(client, instance)
    if container is None:
        return []
    try:
        raw = container.logs(tail=max(1, min(int(tail), 500)), timestamps=False)
    except Exception as exc:
        raise RuntimeError(f"Logs nicht abrufbar: {exc}") from exc
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return [line for line in raw.splitlines() if line.strip()][-500:]


def follow_logs(instance: dict, tail: int = 100):
    """Generator, der Logzeilen folgt (stream=True, follow=True): erst der
    tail-Rückstand, dann live neue Zeilen. Endet, wenn der Container stoppt
    oder der Docker-Stream schließt. Wirft RuntimeError bei Docker-Problemen."""
    client = _get_client()
    container = _get_container(client, instance)
    if container is None:
        return
    try:
        stream = container.logs(tail=max(1, min(int(tail), 500)),
                                follow=True, stream=True, timestamps=False)
        for raw in stream:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            for line in raw.splitlines():
                if line.strip():
                    yield line
    except Exception:
        return  # Stream-Ende (Container gestoppt/entfernt, Docker-Fehler)


# ---------------------------------------------------------------------------
# Ressourcen-Statistik über die Docker-Engine-API
# (funktioniert für Hauptserver UND Instanzen, ohne PID-Namespace-Sharing)
# ---------------------------------------------------------------------------

def _cpu_percent(stats: dict) -> float:
    """CPU-% eines Containers, normalisiert auf die Gesamtkapazität des Hosts
    (0-100 %, wie der Task-Manager).

    Die klassische Docker-Formel ((cpu_delta / system_delta) * online_cpus *
    100) liefert Kern-Verbrauch (100 % = 1 Kern — z. B. 700 % bei 7 belegten
    Kernen). Ohne den online_cpus-Faktor ergibt sich der Anteil am Gesamtsystem
    (system_cpu_usage summiert über alle Kerne) und bleibt im vertrauten
    0-100-%-Bereich."""
    try:
        cpu = stats.get("cpu_stats") or {}
        pre = stats.get("precpu_stats") or {}
        usage = float((cpu.get("cpu_usage") or {}).get("total_usage") or 0.0)
        pre_usage = float((pre.get("cpu_usage") or {}).get("total_usage") or 0.0)
        system = float(cpu.get("system_cpu_usage") or 0.0)
        pre_system = float(pre.get("system_cpu_usage") or 0.0)
        cpu_delta = usage - pre_usage
        system_delta = system - pre_system
        if system_delta <= 0 or cpu_delta <= 0:
            return 0.0
        return round(min((cpu_delta / system_delta) * 100.0, 100.0), 1)
    except Exception:
        return 0.0


def _ram_mb(stats: dict) -> tuple:
    """(genutzte MB, Limit MB); cgroup-v2-Cache (inactive_file) wird abgezogen."""
    mem = stats.get("memory_stats") or {}
    inner = mem.get("stats") or {}
    cache = inner.get("inactive_file")
    if cache is None:
        cache = inner.get("cache") or 0.0
    usage = max(0.0, float(mem.get("usage") or 0.0) - float(cache or 0.0))
    limit = float(mem.get("limit") or 0.0)
    return usage / (1024 * 1024), limit / (1024 * 1024)


def docker_resources() -> dict:
    """CPU/RAM aller laufenden Minecraft-Container (Hauptserver 'minecraft'
    + Instanz-Container 'mc-inst-*'). Liest die Docker-Stats-API über den
    gemounteten Docker-Socket — kein psutil/PID-Namespace nötig.
    Wirft nie: bei Problemen kommt {'found': False, 'reason': ...}."""
    out: dict = {"found": False, "cpu_percent": 0.0, "ram_mb": 0.0, "ram_limit_mb": 0.0,
                 "processes": 0, "containers": [], "reason": None}
    try:
        client = _get_client()
    except RuntimeError as exc:
        out["reason"] = str(exc)
        return out
    try:
        running = client.containers.list(filters={"status": "running"})
    except Exception as exc:
        out["reason"] = f"Container-Liste nicht abrufbar: {exc}"
        return out

    mc = [
        c for c in running
        if (getattr(c, "name", "") or "").startswith("mc-inst-")
        or getattr(c, "name", "") == "minecraft"
        or (getattr(c, "labels", {}) or {}).get("mc-dashboard.managed") == "true"
    ]
    if not mc:
        out["reason"] = "Kein laufender Minecraft-Server-Container"
        return out

    def _one_shot(container) -> dict | None:
        """Ein Stats-Schnappschuss; None bei Fehler (best effort je Container)."""
        try:
            return container.stats(stream=False)
        except Exception:
            return None

    # Der Ein-Schnappschuss-Abruf blockiert im Daemon ~1 s pro Container —
    # parallel anfragen, damit /api/status und der History-Sampler bei vielen
    # Instanzen nicht N Sekunden brauchen. pool.map behält die Reihenfolge,
    # die Summen/Ergebnisse sind identisch zur sequenziellen Variante.
    cpu_total = 0.0
    ram_total = 0.0
    limit_total = 0.0
    workers = max(1, min(len(mc), 8))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for container, stats in zip(mc, pool.map(_one_shot, mc), strict=False):
            if stats is None:
                continue
            pct = round(_cpu_percent(stats), 1)
            used_mb, limit_mb = _ram_mb(stats)
            cpu_total += pct
            ram_total += used_mb
            if limit_mb > 0:
                limit_total += limit_mb
            out["containers"].append({
                "name": getattr(container, "name", "?"),
                "cpu_percent": pct,
                "ram_mb": round(used_mb, 1),
                "ram_limit_mb": round(limit_mb, 1),
            })
    out.update({
        "found": bool(out["containers"]),
        # Summe über alle Container: Anteil an der Gesamtkapazität,
        # durch Rundungen max. 100 %
        "cpu_percent": round(min(cpu_total, 100.0), 1),
        "ram_mb": round(ram_total, 1),
        "ram_limit_mb": round(limit_total, 1),
        "processes": len(out["containers"]),
    })
    return out
