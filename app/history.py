"""Persistenter Statistik-Verlauf (SQLite): CPU/RAM je Container und Spieler-
Zahlen je Instanz über Tage, statt nur der Sparkline der aktuellen Sitzung.
Zusätzlich: Spielzeit je Spieler (Session-Tracking + Tages-Aggregate).

- Sampler (sampler_loop) läuft als Hintergrund-Task der App (Lifespan) und
  schreibt alle HISTORY_INTERVAL Sekunden einen Tick: docker_resources() für
  CPU/RAM je Container, Server-List-Ping für Spielerzahlen je laufender
  Instanz (parallel, 2 s Timeout).
- Spielzeit: der Ping liefert künftig auch die Spielernamen (SLP-Sample;
  bei hide-online-players → RCON 'list' als Fallback). Der Sampler hält
  in-memory offene Sessions (start_ts = erster Sichtkontakt); ein Spieler,
  der 2 Intervalle nicht mehr gesehen wird (oder dessen Instanz stoppt /
  das Dashboard herunterfährt), bekommt eine geschlossene Session in
  player_sessions (end_ts = letzte Sicht) plus anteilige Sekunden in
  player_daily (Tages-Aggregat, bleibt von der Retention ausgenommen —
  Grundlage für „seit Aufzeichnung").
- Retention: Ticks und Sessions älter als HISTORY_RETENTION_DAYS werden beim
  Start und einmal pro Tag gelöscht; player_daily bleibt.
- /api/history liefert serverseitig gebuckette Zeitreihen (max. ~300 Punkte):
  Summen über alle Container/Instanzen + Spieler je Instanz.
- /api/players/playtime liefert das Leaderboard aus player_daily + offenen
  Sessions.

SQLite wird mit WAL betrieben; je Operation wird eine eigene Verbindung
geöffnet (kurze Transaktionen, thread-sicher).
"""
import asyncio
import concurrent.futures
import datetime as dt
import math
import sqlite3
import threading
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
CREATE TABLE IF NOT EXISTS player_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance TEXT NOT NULL,
    player TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER,
    seconds INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sessions_end_ts ON player_sessions(end_ts);
CREATE INDEX IF NOT EXISTS idx_sessions_player ON player_sessions(player);
CREATE TABLE IF NOT EXISTS player_daily (
    day TEXT NOT NULL,
    instance TEXT NOT NULL,
    player TEXT NOT NULL,
    seconds INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, instance, player)
);
"""

_TICK_PING_TIMEOUT = 2.0
_RCON_LIST_TIMEOUT = 3.0
_MAX_POINTS = 300
# Sicherheitsventile für die Session-Logik
_MAX_NAMES_PER_INSTANCE = 200
_SESSION_MISSES_TO_CLOSE = 2
_MAX_PLAYERS_OUTPUT = 2000

_sessions_lock = threading.Lock()
# (instance, player) -> {"start": ts, "last_seen": ts, "interval": s}
_OPEN_SESSIONS: dict = {}
_miss_counts: dict = {}


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
    """Ticks und Spielzeit-Sessions außerhalb der Aufbewahrungszeit löschen.
    player_daily bleibt (winziges Tages-Aggregat, „seit Aufzeichnung")."""
    days = retention_days if retention_days is not None else settings.history_retention_days
    cutoff = int(time.time()) - days * 86400
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        cur = conn.execute("DELETE FROM player_sessions WHERE end_ts < ?", (cutoff,))
        removed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return removed


def sample_tick(now: int | None = None) -> int:
    """Ein Sampling-Durchlauf (blockierend; wird per to_thread aufgerufen):
    Docker-Stats + Spieler-Pings → ein Tick in die DB, Session-Logik für die
    Spielzeit. Rückgabe: Zeilenzahl der Samples."""
    from . import runtime  # lazy, vermeidet Import-Zirkel
    ts = int(now or time.time())
    interval = max(10, settings.history_interval)
    rows: list[tuple] = []
    ping_targets: list[tuple[str, dict, tuple]] = []
    running_ids: set = set()
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
            running_ids.add(inst_id)
            try:
                inst = instances_get(inst_id)
                ping_targets.append((inst_id, inst, instance_ping_target(inst_id)))
            except Exception:
                continue  # Instanz gelöscht? nächsten Tick neu prüfen
    names_by_instance: dict = {}
    if ping_targets:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_ping_players, inst, target): inst_id
                       for inst_id, inst, target in ping_targets}
            for future in concurrent.futures.as_completed(futures):
                inst_id = futures[future]
                try:
                    players = future.result()
                except Exception:
                    players = None
                if players is not None:
                    online, maximum, names = players
                    rows.append((ts, "players", inst_id, None, None,
                                 online, maximum))
                    names_by_instance[inst_id] = names
    closed = apply_tick(ts, names_by_instance, running_ids, interval)
    if closed:
        try:
            flush_sessions(closed)
        except Exception:
            pass  # Session-Persistenz darf den Sampler nie stoppen
    record_samples(rows)
    return len(rows)


def instances_get(inst_id: str):
    """Instanz-Metadaten (lazy import, wirft bei Unbekannten)."""
    from . import instances  # lazy, vermeidet Import-Zirkel
    return instances.get_instance(inst_id)


def instance_ping_target(inst_id: str):
    """Host/Port für den Spieler-Ping einer Instanz aus Sicht des Dashboards
    (identisch zur Logik in main._instance_ping_host)."""
    from . import runtime  # lazy, vermeidet Import-Zirkel
    if not Path("/.dockerenv").exists():
        return "127.0.0.1", int(instances_get(inst_id)["port"])
    # Im Docker-Netz ist die Instanz unter ihrem Container-Namen erreichbar
    return runtime.container_name(inst_id), 25565


def _rcon_list_names(inst: dict) -> list:
    """Spieler-Namen per RCON 'list' (Fallback für hide-online-players).
    Fehler werden geschluckt — Spielzeit ist best effort."""
    from . import rcon as rcon_mod  # lazy, vermeidet Import-Zirkel
    from . import runtime
    try:
        host, port = runtime.rcon_target(inst)
        output = rcon_mod.command(host, port, runtime.rcon_secret(inst),
                                  "list", timeout=_RCON_LIST_TIMEOUT)
        parsed = rcon_mod.parse_list_output(output)
        if parsed:
            return [n for n in parsed.get("names") or [] if n]
    except Exception:
        pass
    return []


def _ping_players(inst: dict, target) -> tuple | None:
    """SLP-Ping → (online, max, names) oder None.
    Namen aus dem SLP-Sample; wenn online > 0 aber Sample leer ist
    (hide-online-players=true), Fallback RCON 'list'."""
    from . import minecraft
    if not target:
        return None
    host, port = target
    ping = minecraft.server_status(host, port, _TICK_PING_TIMEOUT)
    players = ping.get("players") or {}
    online, maximum = players.get("online"), players.get("max")
    if not isinstance(online, int):
        return None
    names: list = []
    if online > 0:
        sample = players.get("sample") or []
        names = [str(p.get("name")).strip() for p in sample
                 if isinstance(p, dict) and p.get("name")]
        if not names:
            names = _rcon_list_names(inst)
    return online, maximum if isinstance(maximum, int) else 0, names[:_MAX_NAMES_PER_INSTANCE]


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
    Retention-Prune einmal pro Tag. Beim Stoppen werden offene Spielzeit-
    Sessions geschlossen (Lifespan-Finally-Analogie der anderen Tasks)."""
    interval = max(10, settings.history_interval)
    next_prune = 0.0
    try:
        while True:
            try:
                await asyncio.to_thread(sample_tick)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Verlauf darf das Dashboard nie stören — nächster Tick probiert es
                import logging
                logging.getLogger("dashboard").exception(
                    "Verlauf-Sampling fehlgeschlagen")
            now = time.time()
            if now >= next_prune:
                next_prune = now + 86400
                try:
                    await asyncio.to_thread(prune)
                except Exception:
                    pass
            await asyncio.sleep(interval)
    finally:
        try:
            close_all_sessions()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Spielzeit je Spieler: Session-Logik (in-memory) + Persistenz
# ---------------------------------------------------------------------------

def _day_of(ts: int) -> str:
    """Lokales Kalenderdatum als ISO-String (Tages-Aggregat player_daily)."""
    return dt.datetime.fromtimestamp(int(ts)).date().isoformat()


def _split_days(start: int, end: int, seconds: int) -> list:
    """Sekunden einer Session anteilig auf Kalendertage verteilen
    (Rundungsrest auf den letzten Tag)."""
    if end <= start or seconds <= 0:
        return [(_day_of(start), seconds)]
    spans = []
    day = dt.datetime.fromtimestamp(start).date()
    last = dt.datetime.fromtimestamp(end).date()
    while day <= last:
        day_start = int(dt.datetime.combine(day, dt.time.min).timestamp())
        overlap = max(0, min(end, day_start + 86400) - max(start, day_start))
        if overlap > 0:
            spans.append((day.isoformat(), overlap))
        day += dt.timedelta(days=1)
    if not spans:
        return [(_day_of(start), seconds)]
    span_total = sum(o for _, o in spans)
    out = []
    allocated = 0
    for idx, (day_str, overlap) in enumerate(spans):
        if idx == len(spans) - 1:
            out.append((day_str, seconds - allocated))
        else:
            part = int(seconds * overlap / max(1, span_total))
            allocated += part
            out.append((day_str, part))
    return out


def _close_session_locked(key, now_ts: int) -> dict:
    """Session schließen (Lock muss gehalten werden): end_ts = letzte Sicht;
    Gutschrift = (letzte Sicht - Start) + ein Intervall - der Spieler war
    zwischen Start und letzter Sicht nachweislich online, plus eine Puffer-
    Periode für kurze Besuche."""
    session = _OPEN_SESSIONS.pop(key)
    _miss_counts.pop(key, None)
    start = int(session["start"])
    end = int(session["last_seen"])
    seconds = max(0, end - start) + int(session.get("interval") or 0)
    return {"instance": key[0], "player": key[1],
            "start": start, "end": end, "seconds": seconds}


def _apply_tick_locked(ts: int, names_by_instance: dict, running_ids,
                       interval: int) -> list:
    """Session-Update für einen Tick (Lock muss gehalten werden).
    Gesehen → verlängern (Lücke ≤ 2 Intervalle); sonst Miss-Zähler; ab 2
    Misses (oder gestoppter Instanz) wird geschlossen. Zu große Lücken
    schließen die alte Session und öffnen eine neue. running_ids=None
    überspringt das Schließen gestoppter Instanzen (Wrapper-Fall)."""
    closed: list = []
    for instance, names in names_by_instance.items():
        seen = set()
        for name in list(names or [])[:_MAX_NAMES_PER_INSTANCE]:
            name = str(name).strip()[:64]
            if not name:
                continue
            key = (str(instance), name)
            if key in seen:
                continue
            seen.add(key)
            session = _OPEN_SESSIONS.get(key)
            if session and ts - int(session["last_seen"]) <= 2 * max(1, interval):
                session["last_seen"] = int(ts)
                session["interval"] = interval
                _miss_counts[key] = 0
            elif session:
                closed.append(_close_session_locked(key, ts))
                _OPEN_SESSIONS[key] = {"start": int(ts), "last_seen": int(ts),
                                       "interval": interval}
                _miss_counts[key] = 0
            else:
                _OPEN_SESSIONS[key] = {"start": int(ts), "last_seen": int(ts),
                                       "interval": interval}
                _miss_counts[key] = 0
        for key in [k for k in _OPEN_SESSIONS
                    if k[0] == str(instance) and k not in seen]:
            _miss_counts[key] = _miss_counts.get(key, 0) + 1
            if _miss_counts[key] >= _SESSION_MISSES_TO_CLOSE:
                closed.append(_close_session_locked(key, ts))
    if running_ids is None:
        return closed
    # Instanz gestoppt/verschwunden → offene Sessions sofort schließen
    for key in [k for k in _OPEN_SESSIONS if k[0] not in running_ids]:
        closed.append(_close_session_locked(key, ts))
    return closed


def apply_tick(ts: int, names_by_instance: dict, running_ids,
               interval: int) -> list:
    """Session-Logik eines Ticks (thread-sicher). running_ids=None überspringt
    das Schließen gestoppter Instanzen. Rückgabe: geschlossene Sessions
    (Dicts); das Schreiben übernimmt flush_sessions()."""
    with _sessions_lock:
        return _apply_tick_locked(ts, names_by_instance, running_ids, interval)


def update_sessions(ts: int, instance: str, names: list, interval: int) -> list:
    """Bequemlichkeits-Wrapper (Tests/externe Aufrufer): Ein Tick für genau
    eine Instanz inkl. Flush der geschlossenen Sessions. Andere Instanzen
    werden nicht berührt (running_ids=None)."""
    closed = apply_tick(ts, {str(instance): list(names or [])},
                        None, interval)
    flush_sessions(closed)
    return closed


def close_all_sessions(now_ts: int | None = None) -> int:
    """Alle offenen Sessions schließen (Dashboard-Shutdown, Tests)."""
    now = int(now_ts or time.time())
    with _sessions_lock:
        closed = [_close_session_locked(key, now) for key in list(_OPEN_SESSIONS)]
    try:
        flush_sessions(closed)
    except Exception:
        pass  # Shutdown darf an der Persistenz nicht scheitern
    return len(closed)


def open_sessions() -> list:
    """Snapshot der offenen Sessions (Diagnose/Tests)."""
    with _sessions_lock:
        return [(k[0], k[1], dict(v)) for k, v in _OPEN_SESSIONS.items()]


def reset_sessions_for_tests() -> None:
    """In-Memory-Sessions verwerfen (nur Tests; ohne DB-Flush)."""
    with _sessions_lock:
        _OPEN_SESSIONS.clear()
        _miss_counts.clear()


def flush_sessions(sessions: list, db_path: Path | None = None) -> int:
    """Geschlossene Sessions schreiben: je Session eine Zeile in
    player_sessions plus anteilige Sekunden in player_daily (Upsert)."""
    if not sessions:
        return 0
    session_rows = []
    daily: dict = {}
    for s in sessions:
        start, end, seconds = int(s["start"]), int(s["end"]), int(s["seconds"])
        if not s.get("player") or not s.get("instance"):
            continue
        session_rows.append((s["instance"], s["player"], start, end, seconds))
        for day, part in _split_days(start, end, seconds):
            if part <= 0:
                continue
            key = (day, s["instance"], s["player"])
            daily[key] = daily.get(key, 0) + part
    if not session_rows:
        return 0
    with _connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO player_sessions (instance, player, start_ts, end_ts, "
            "seconds) VALUES (?,?,?,?,?)", session_rows)
        conn.executemany(
            "INSERT INTO player_daily (day, instance, player, seconds) "
            "VALUES (?,?,?,?) ON CONFLICT(day, instance, player) "
            "DO UPDATE SET seconds = seconds + excluded.seconds",
            [(d, i, p, s) for (d, i, p), s in daily.items()])
    return len(session_rows)


def query_playtime(hours: float | str | None = "24", instance: str | None = None,
                   db_path: Path | None = None, now: int | None = None) -> dict:
    """Leaderboard der Spielzeit: player_daily (Tages-Aggregate) über das
    Zeitfenster plus laufende, offene Sessions aus dem Sampler.
    hours=None/'all' → seit Aufzeichnung. Rückgabe: {"players": [
    {player, seconds, last_seen, per_instance}]}."""
    now = int(now or time.time())
    all_time = hours in (None, "all")
    try:
        hours_num = int(float(str(hours))) if not all_time else 0
    except (TypeError, ValueError):
        hours_num = 0
    window_start = 0 if all_time else now - hours_num * 3600
    day_min = "" if all_time else _day_of(window_start)
    players: dict = {}

    def entry(name: str) -> dict:
        return players.setdefault(name, {"player": name, "seconds": 0,
                                         "last_seen": None, "per_instance": {}})

    with _connect(db_path) as conn:
        params: list = [day_min]
        inst_sql = ""
        if instance:
            inst_sql = " AND instance = ?"
            params.append(instance)
        rows = conn.execute(
            "SELECT player, instance, SUM(seconds) FROM player_daily "
            "WHERE day >= ?" + inst_sql + " GROUP BY player, instance",
            params).fetchall()
        sess_params: list = [window_start]
        sess_inst_sql = ""
        if instance:
            sess_inst_sql = " AND instance = ?"
            sess_params.append(instance)
        sess_rows = conn.execute(
            "SELECT instance, player, end_ts FROM player_sessions "
            "WHERE (end_ts IS NULL OR end_ts >= ?)" + sess_inst_sql,
            sess_params).fetchall()
    for player, inst, seconds in rows:
        e = entry(player)
        e["seconds"] += int(seconds or 0)
        e["per_instance"][inst] = e["per_instance"].get(inst, 0) + int(seconds or 0)
    for _inst, player, end_ts in sess_rows:
        e = entry(player)
        if end_ts is None:
            continue  # offene Session kommt aus dem Sampler-Speicher
        if end_ts > (e["last_seen"] or 0):
            e["last_seen"] = int(end_ts)
    with _sessions_lock:
        for key, session in _OPEN_SESSIONS.items():
            inst, player = key
            if instance and inst != instance:
                continue
            e = entry(player)
            if now > max(int(session["start"]), window_start):
                partial = now - max(int(session["start"]), window_start)
                e["seconds"] += partial
                e["per_instance"][inst] = e["per_instance"].get(inst, 0) + partial
            if int(session["last_seen"]) > (e["last_seen"] or 0):
                e["last_seen"] = int(session["last_seen"])
    out = sorted(players.values(), key=lambda e: (-e["seconds"], e["player"]))
    return {"hours": "all" if all_time else hours_num,
            "players": out[:_MAX_PLAYERS_OUTPUT], "now": now}


__all__ = [
    "apply_tick",
    "close_all_sessions",
    "flush_sessions",
    "open_sessions",
    "prune",
    "query_playtime",
    "query_series",
    "record_samples",
    "reset_sessions_for_tests",
    "sample_tick",
    "sampler_loop",
    "update_sessions",
]
