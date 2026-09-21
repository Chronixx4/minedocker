"""Konfiguration über Umgebungsvariablen + Ziel-Version-Erkennung."""
import json
import os
import re
import time
from pathlib import Path

DEFAULT_MC_VERSION = "26.3"
ALLOWED_LOADERS = ("fabric", "forge", "neoforge", "quilt", "bukkit", "spigot", "paper")


class Settings:
    def __init__(self) -> None:
        self.host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
        self.port = int(os.getenv("DASHBOARD_PORT", "8080"))
        self.mods_dir = Path(os.getenv("MODS_DIR", "/data/mods")).resolve()
        self.mc_host = os.getenv("MC_HOST", "minecraft")
        self.mc_port = int(os.getenv("MC_PORT", "25565"))
        self.mc_version = os.getenv("MC_VERSION", "").strip()
        self.mod_loader = os.getenv("MOD_LOADER", "fabric").strip().lower()
        self.loader_version = os.getenv("LOADER_VERSION", "").strip()
        self.api_key = os.getenv("DASHBOARD_API_KEY", "").strip()
        self.cors_origins = [
            origin.strip()
            for origin in os.getenv("CORS_ORIGINS", "*").split(",")
            if origin.strip()
        ]
        self.modrinth_api = os.getenv("MODRINTH_API", "https://api.modrinth.com/v2")
        self.curseforge_api = (os.getenv("CURSEFORGE_API", "").strip().rstrip("/")
                               or "https://api.curseforge.com/v1")
        # Kostenloser CurseForge-API-Key: https://console.curseforge.com
        self.cf_api_key = os.getenv("CF_API_KEY", "").strip()
        self.user_agent = "mc-dashboard/1.0 (self-hosted Minecraft admin panel)"
        # Multi-Server: separater Speicherort + Port-Bereich pro Instanz
        self.instances_dir = Path(os.getenv("INSTANCES_DIR", "/data/instances")).resolve()
        self.instances_port_base = int(os.getenv("INSTANCES_PORT_BASE", "25570"))
        self.instances_memory = os.getenv("INSTANCES_MEMORY", "2G").strip()
        # Host-Pfad des Instanz-Ordners (für Docker-Binds); leer = automatische
        # Erkennung über /proc/self/mountinfo
        self.instances_host_dir = os.getenv("INSTANCES_HOST_DIR", "").strip()
        # RCON-Konsole: 'whitelist' erlaubt nur freigegebene Befehle (Standard),
        # 'free' erlaubt beliebige Befehle (bewusst freigeschaltet).
        mode = os.getenv("RCON_CONSOLE_MODE", "whitelist").strip().lower()
        self.rcon_console_mode = mode if mode in ("whitelist", "free") else "whitelist"
        # Zusätzlich freigegebene Befehle (Komma-separiert), z. B. "whitelist,say"
        self.rcon_console_whitelist = [
            token.strip().lower()
            for token in os.getenv("RCON_CONSOLE_WHITELIST", "").split(",")
            if token.strip()
        ]
        # Persistenter Statistik-Verlauf (SQLite): DB-Pfad, Sampling-Intervall
        # und Aufbewahrung. Default: neben INSTANCES_DIR (im Container /data).
        env_history_db = os.getenv("HISTORY_DB", "").strip()
        self.history_db = Path(env_history_db).resolve() if env_history_db \
            else (self.instances_dir.parent / "history.db")
        try:
            self.history_interval = max(10, int(os.getenv("HISTORY_INTERVAL", "30")))
        except ValueError:
            self.history_interval = 30
        try:
            self.history_retention_days = max(1, int(os.getenv("HISTORY_RETENTION_DAYS", "30")))
        except ValueError:
            self.history_retention_days = 30
        # Crash-Watchdog/Alerts (optional): Discord-kompatibler Webhook und/oder
        # Telegram; ALERT_EVENTS wählt die Ereignisse (crash/start/stop).
        self.alert_webhook_url = os.getenv("ALERT_WEBHOOK_URL", "").strip()
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.alert_events = os.getenv("ALERT_EVENTS", "crash,start,stop").strip()
        if self.mod_loader not in ALLOWED_LOADERS:
            self.mod_loader = "fabric"


settings = Settings()

_version_cache: dict[str, object] = {"value": None, "ts": 0.0}
_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")


def current_game_version() -> str:
    """Ziel-Minecraft-Version: Env-Variablen haben Vorrang, sonst aus
    /data/version.json ermitteln (wird vom Vanilla/Fabric-Server geschrieben),
    sonst Fallback-Default. Cache 60 s."""
    if settings.mc_version:
        return settings.mc_version
    now = time.time()
    cached = _version_cache["value"]
    if cached and now - float(str(_version_cache["ts"])) < 60:
        return str(cached)
    version = None
    candidates = [settings.mods_dir.parent / "version.json", Path("/data/version.json")]
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            found = data.get("id") or data.get("name")
            if isinstance(found, str) and _VERSION_RE.match(found.strip()):
                version = found.strip()
                break
        except Exception:
            continue
    _version_cache.update(value=version or DEFAULT_MC_VERSION, ts=now)
    return str(_version_cache["value"])
