"""Persistenter Statistik-Verlauf (SQLite): CPU/RAM je Container und Spieler-
Zahlen je Instanz über Tage, statt nur der Sparkline der aktuellen Sitzung.

- Sampler (sampler_loop) läuft als Hintergrund-Task der App (Lifespan) und
  schreibt alle HISTORY_INTERVAL Sekunden einen Tick: docker_resources() für
  CPU/RAM je Container, Server-List-Ping für Spielerzahlen je laufender
  Instanz (parallel, 2 s Timeout).
- Retention: Ticks älter als HISTORY_RETENTION_DAYS werden beim Start und
  einmal pro Tag gelöscht.
- /api/history liefert serverseitig gebuckette Zeitreihen (max. ~300 Punkte):
  Summen über alle Container/Instanzen + Spieler je Instanz.

SQLite wird mit WAL betrieben; je Operation wird eine eigene Verbindung
geöffnet (kurze Transaktionen, thread-sicher).
"""
import asyncio
import concurrent.futures
import math
import sqlite3
import time
from pathlib import Path

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    cpu REAL,
    ram_mb REAL,
    players INTEGER,
    players_max INTEGER
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE INDEX IF NOT EXISTS idx_samples_kind_key_ts ON samples(kind, key, ts);
"""

_TICK_PING_TIMEOUT = 2.0
_MAX_POINTS = 300


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else settings.history_db
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)  # idempotent (IF NOT EXISTS)
    return conn


def record_samples(rows: list, db_path: Path | None = None) -> int:
    """Ticks schreiben: (ts, kind, key, cpu, ram_mb, players, players_max)."""
    rows = [r for r in rows if r and len(r) == 7]
    if not rows:
        return 0
    with _connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO samples (ts, kind, key, cpu, ram_mb, players, players_max) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def prune(retention_days: int | None = None,
          db_path: Path | None = None) -> int:
    """Ticks außerhalb der Aufbewahrungszeit löschen."""
    days = retention_days if retention_days is not None else settings.history_retention_days
    cutoff = int(time.time()) - days * 86400
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def sample_tick() -> int:
    """Ein Sampling-Durchlauf (blockierend; wird per to_thread aufgerufen):
    Docker-Stats + Spieler-Pings → ein Tick in die DB. Rückgabe: Zeilenzahl."""
    from . import runtime  # lazy, vermeidet Import-Zirkel
    ts = int(time.time())
    rows: list[tuple] = []
    ping_targets: list[tuple[str, tuple]] = []
    try:
        resources = runtime.docker_resources()
    except Exception:
        resources = {"containers": []}
    for container in resources.get("containers") or []:
        name = str(container.get("name") or "")
        if not name:
            continue
        rows.append((ts, "container", name,
                     float(container.get("cpu_percent") or 0.0),
                     float(container.get("ram_mb") or 0.0), None, None))
        if name.startswith("mc-inst-"):
            inst_id = name[len("mc-inst-"):]
            try:
                ping_targets.append((inst_id, instance_ping_target(inst_id)))
            except Exception:
                continue  # Instanz gelöscht? nächsten Tick neu prüfen
    if ping_targets:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_ping_players, target): inst_id
                       for inst_id, target in ping_targets}
            for future in concurrent.futures.as_completed(futures):
                inst_id = futures[future]
                try:
                    players = future.result()
                except Exception:
                    players = None
                if players is not None:
                    rows.append((ts, "players", inst_id, None, None,
                                 players[0], players[1]))
    record_samples(rows)
    return len(rows)


def instance_ping_target(inst_id: str):
    """Host/Port für den Spieler-Ping einer Instanz aus Sicht des Dashboards
    (identisch zur Logik in main._instance_ping_host)."""
    from . import instances, runtime  # lazy, vermeidet Import-Zirkel
    if not Path("/.dockerenv").exists():
        return "127.0.0.1", int(instances.get_instance(inst_id)["port"])
    # Im Docker-Netz ist die Instanz unter ihrem Container-Namen erreichbar
    return runtime.container_name(inst_id), 25565


def _ping_players(target):
    """SLP-Ping → (online, max) oder None."""
    from . import minecraft
    if not target:
        return None
    host, port = target
    ping = minecraft.server_status(host, port, _TICK_PING_TIMEOUT)
    players = ping.get("players") or {}
    online, maximum = players.get("online"), players.get("max")
    if not isinstance(online, int):
        return None
    return online, maximum if isinstance(maximum, int) else 0


def _bucket_seconds(hours: float) -> int:
    """Bucket-Größe: Ziel ~300 Punkte, gerundet auf 60 s."""
    raw = hours * 3600 / _MAX_POINTS
    return max(60, math.ceil(raw / 60.0) * 60)


def query_series(hours: float = 24, db_path: Path | None = None) -> dict:
    """Zeitreihen aus dem Verlauf, serverseitig gebuckett (Mittel je Bucket):
    points = Summen über alle Container (CPU/RAM) und Instanzen (Spieler),
    instances = Spieler-Zahlen je Instanz-Key."""
    since = int(time.time()) - int(hours * 3600)
    bucket = _bucket_seconds(hours)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ts, kind, key, cpu, ram_mb, players FROM samples "
            "WHERE ts >= ? ORDER BY ts", (since,)).fetchall()

    def _bucketize(pairs: list) -> list:
        """[(ts, wert)] → [{'ts': Bucket-Start, 'value': Mittelwert je Bucket}]."""
        buckets: dict[int, list[float]] = {}
        for ts, value in pairs:
            buckets.setdefault(ts - (ts % bucket), []).append(value)
        return [{"ts": start, "value": sum(values) / len(values)}
                for start, values in sorted(buckets.items())]

    raw: dict[int, dict[str, float]] = {}
    per_key: dict[str, dict[int, int]] = {}
    for ts, kind, key, cpu, ram_mb, players in rows:
        tick = raw.setdefault(ts, {"cpu": 0.0, "ram_mb": 0.0, "players": 0})
        if kind == "container":
            tick["cpu"] += cpu or 0.0
            tick["ram_mb"] += ram_mb or 0.0
        elif kind == "players":
            tick["players"] += players or 0
            series = per_key.setdefault(str(key), {})
            series[ts] = series.get(ts, 0) + (players or 0)

    cpu_map = {e["ts"]: e["value"] for e in
               _bucketize([(ts, t["cpu"]) for ts, t in sorted(raw.items())])}
    ram_map = {e["ts"]: e["value"] for e in
               _bucketize([(ts, t["ram_mb"]) for ts, t in sorted(raw.items())])}
    players_map = {e["ts"]: e["value"] for e in
                   _bucketize([(ts, t["players"]) for ts, t in sorted(raw.items())])}
    points = [{"ts": ts,
               "cpu": round(cpu_map.get(ts, 0.0), 2),
               "ram_mb": round(ram_map.get(ts, 0.0), 1),
               "players": round(players_map.get(ts, 0.0), 2)}
              for ts in sorted(set(cpu_map) | set(ram_map) | set(players_map))]

    instances_out = {}
    for key, series in sorted(per_key.items()):
        instances_out[key] = [
            {"ts": e["ts"], "players": round(e["value"], 2)}
            for e in _bucketize(sorted(series.items()))]

    return {"hours": int(hours), "bucket_seconds": bucket,
            "points": points, "instances": instances_out}


async def sampler_loop() -> None:
    """Hintergrund-Task (Lifespan): Sample-Tick alle HISTORY_INTERVAL Sekunden,
    Retention-Prune einmal pro Tag."""
    interval = max(10, settings.history_interval)
    next_prune = 0.0
    while True:
        try:
            await asyncio.to_thread(sample_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Verlauf darf das Dashboard nie stören — nächster Tick probiert es
            import logging
            logging.getLogger("dashboard").exception("Verlauf-Sampling fehlgeschlagen")
        now = time.time()
        if now >= next_prune:
            next_prune = now + 86400
            try:
                await asyncio.to_thread(prune)
            except Exception:
                pass
        await asyncio.sleep(interval)


__all__ = [
    "prune",
    "query_series",
    "record_samples",
    "sample_tick",
    "sampler_loop",
]
