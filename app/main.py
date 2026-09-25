"""Einstiegspunkt: FastAPI-App mit API-Routen, CORS und statischem Frontend."""
import asyncio
import json
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import auth as auth_mod
from . import backups as backups_mod
from . import catalog, curseforge, instances, modrinth, packs, runtime, worlds
from . import datapacks as datapacks_mod
from . import filebrowser as filebrowser_mod
from . import gamerules as gamerules_mod
from . import history as history_mod
from . import icon as icon_mod
from . import rcon as rcon_mod
from . import scheduler as scheduler_mod
from . import updates as updates_mod
from . import watchdog as watchdog_mod
from . import whitelist as whitelist_mod
from .config import ALLOWED_LOADERS, current_game_version, settings
from .minecraft import server_status
from .security import auth_guard, safe_mods_path, validate_identifier

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("dashboard")

# Intervall des Job-Spiegels (Sekunden): laufende Installationen überleben
# einen Dashboard-Neustart mit Status "Abgebrochen (Neustart)" auf der Platte
_JOB_FLUSH_SECONDS = 5.0


async def _job_flusher():
    """Spiegelt alle Job-Zustände regelmäßig auf Platte (Job-Persistenz)."""
    while True:
        await asyncio.sleep(_JOB_FLUSH_SECONDS)
        try:
            await asyncio.to_thread(modrinth.snapshot_jobs)
        except Exception:
            pass  # Spiegel ist optional — Flush-Fehler dürfen nichts brechen


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startet den Verlauf-Sampler (SQLite), den Job-Spiegel-Flush und den
    Crash-Watchdog-Thread und stoppt sie sauber beim Herunterfahren. Im
    TestClient (ohne 'with') läuft die Lifespan nicht — gewollt."""
    try:
        restored = await asyncio.to_thread(modrinth.restore_jobs)
        if restored:
            logger.info("Job-Spiegel wiederhergestellt: %d Jobs (%d abgebrochen)",
                        restored, sum(
                            1 for j in modrinth.JOBS.values()
                            if j.get("phase") == "Abgebrochen (Neustart)"))
    except Exception:
        logger.exception("Job-Spiegel konnte nicht geladen werden")
    history_task = asyncio.create_task(history_mod.sampler_loop())
    flush_task = asyncio.create_task(_job_flusher())
    watchdog_mod.STOP.clear()
    watchdog_thread = threading.Thread(
        target=watchdog_mod.watchdog_loop, args=(watchdog_mod.STOP,),
        daemon=True, name="crash-watchdog")
    watchdog_thread.start()
    scheduler_mod.STOP.clear()
    scheduler_thread = threading.Thread(
        target=scheduler_mod.scheduler_loop, args=(scheduler_mod.STOP,),
        daemon=True, name="scheduler")
    scheduler_thread.start()
    try:
        yield
    finally:
        watchdog_mod.STOP.set()
        scheduler_mod.STOP.set()
        for task in (history_task, flush_task):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass  # Hintergrund-Tasks sind optional — Shutdown darf nicht blockieren


app = FastAPI(title="Minecraft Dashboard", docs_url=None, redoc_url=None,
              openapi_url=None, lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)

# Alle /api-Routen (außer /api/health und /api/auth/*) durchlaufen den
# kombinierten Guard: DASHBOARD_API_KEY (Admin) ODER Login-Cookie; Rollen-
# Prüfung (schreibende Methoden nur für Admin) passiert im Guard selbst.
api = APIRouter(prefix="/api", dependencies=[Depends(auth_guard)])


class DownloadRequest(BaseModel):
    project_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    version_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    overwrite: bool = False
    # Optional: in den mods-Ordner einer bestimmten Instanz laden
    # (statt in den zentralen mods-Ordner des Hauptservers)
    instance_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9]{1,32}$")
    # Pflicht-Abhängigkeiten automatisch mitinstallieren (nur Modrinth)
    with_dependencies: bool = True


class ToggleModRequest(BaseModel):
    enabled: bool


class CreateInstanceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    loader: str = Field(min_length=1, max_length=32)
    game_version: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._-]{1,32}$")
    loader_version: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    port: int | None = Field(default=None, ge=1024, le=65535)
    memory: str | None = Field(default=None, pattern=r"^\d{1,4}[GgMm]$")
    accept_eula: bool = False
    # Tag-Validierung (Regex, max 8, Dedupe) macht das Backend in instances.py
    tags: list[str] | None = None


class InstallPackRequest(BaseModel):
    project_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    version_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    force: bool = False
    # Quelle der Pack-Suche: modrinth (Standard) oder curseforge.
    # CurseForge: project_id = Mod-ID oder Slug, version_id = numerische file_id.
    source: str | None = Field(default=None, pattern=r"^(modrinth|curseforge)$")
    file_id: str | None = Field(default=None, pattern=r"^\d{1,12}$")


class CreateFromPackRequest(BaseModel):
    """Server direkt aus einem Modpack erstellen: MC-Version + Loader stammen
    aus den Pack-Metadaten, das Pack wird in die neue Instanz installiert."""
    project_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    source: str = Field(default="modrinth", pattern=r"^(modrinth|curseforge)$")
    version_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    file_id: str | None = Field(default=None, pattern=r"^\d{1,12}$")
    name: str | None = Field(default=None, min_length=1, max_length=64)
    memory: str | None = Field(default=None, pattern=r"^\d{1,4}[GgMm]$")
    port: int | None = Field(default=None, ge=1024, le=65535)
    accept_eula: bool = False
    prefer_server_pack: bool = True  # CurseForge: Server-Pack-Datei bevorzugen


class ConfigProperty(BaseModel):
    """Einzelne Zeile aus server.properties (key=value)."""
    key: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    value: str = Field(default="", max_length=512, pattern=r"^[^\r\n]*$")


class ConfigRequest(BaseModel):
    properties: list[ConfigProperty] = Field(max_length=200)


class PackUpdateRequest(BaseModel):
    """Pack-Update in bestehende Instanz: ohne Angabe wird die neueste
    (kompatible) Version installiert."""
    version_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    file_id: str | None = Field(default=None, pattern=r"^\d{1,12}$")


class RconRequest(BaseModel):
    """Spieler-Verwaltung über RCON: op/deop/kick/ban/pardon/list/banlist."""
    action: str = Field(pattern=r"^(op|deop|kick|ban|pardon|list|banlist)$")
    target: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]{1,16}$")
    reason: str | None = Field(default=None, max_length=100,
                                  pattern=r"^[^\r\n]*$")


class CloneInstanceRequest(BaseModel):
    """Optionaler Name für den Klon; ohne Name wird '{Name} 2' abgeleitet."""
    name: str | None = Field(default=None, min_length=1, max_length=64)


class ConsoleRequest(BaseModel):
    """Freies RCON-Kommando (Konsole)."""
    command: str = Field(min_length=1, max_length=256)


class WhitelistRequest(BaseModel):
    """whitelist.json vollständig ersetzen (Namen)."""
    entries: list[str] = Field(default_factory=list, max_length=200)


class UpdateModsRequest(BaseModel):
    """Zu aktualisierende Mods; ohne Liste = alle mit verfügbarem Update."""
    filenames: list[str] | None = Field(default=None, max_length=200)


class RestartScheduleModel(BaseModel):
    """Täglicher Neustart zur Uhrzeit (Container-Lokalzeit) mit RCON-Vorwarnung."""
    enabled: bool | None = None
    time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    warn_minutes: int | None = Field(default=None, ge=0, le=30)


class StopScheduleModel(BaseModel):
    """Täglicher Stopp zur Uhrzeit (Container-Lokalzeit) mit RCON-Vorwarnung."""
    enabled: bool | None = None
    time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    warn_minutes: int | None = Field(default=None, ge=0, le=30)


class BackupScheduleModel(BaseModel):
    """Zeitgesteuertes Backup je Instanz mit eigener Rotation."""
    enabled: bool | None = None
    interval_hours: int | None = Field(default=None, ge=1, le=168)
    keep: int | None = Field(default=None, ge=1, le=20)


class UpdateCheckScheduleModel(BaseModel):
    """Geplanter Mod-Update-Check; bei Updates Alert (Ereignis 'update')."""
    enabled: bool | None = None
    interval_hours: int | None = Field(default=None, ge=1, le=168)


class ScheduleModel(BaseModel):
    """Zeitplan einer Instanz (Scheduler): Auto-Start beim Dashboard-Start,
    täglicher Neustart, täglicher Stopp, zeitgesteuerte Backups, geplanter
    Update-Check."""
    auto_start: bool | None = None
    restart: RestartScheduleModel | None = None
    stop: StopScheduleModel | None = None
    backup: BackupScheduleModel | None = None
    update_check: UpdateCheckScheduleModel | None = None


class UpdateInstanceRequest(BaseModel):
    """Instanz-Einstellungen ändern (Name, RAM, JVM-Flags, Tags, Port, Zeitplan)."""
    name: str | None = Field(default=None, min_length=1, max_length=64)
    memory: str | None = Field(default=None, pattern=r"^\d{1,4}[GgMm]$")
    jvm_opts: str | None = Field(default=None, max_length=2000)
    use_aikar: bool | None = None
    # Tag-/Port-Validierung macht das Backend in instances.py
    tags: list[str] | None = None
    port: int | None = Field(default=None, ge=1024, le=65535)
    schedule: ScheduleModel | None = None


class AuthSetupRequest(BaseModel):
    """Ersten Admin anlegen (nur solange kein Benutzer existiert)."""
    username: str = Field(min_length=1, max_length=32,
                          pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    password: str = Field(min_length=8, max_length=128)


class AuthLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32,
                          pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    password: str = Field(min_length=1, max_length=128)


class AuthUserCreateRequest(AuthSetupRequest):
    role: str = Field(default="viewer", pattern=r"^(admin|viewer)$")


class AuthUserPatchRequest(BaseModel):
    role: str | None = Field(default=None, pattern=r"^(admin|viewer)$")
    password: str | None = Field(default=None, min_length=8, max_length=128)


class AuthChangePasswordRequest(BaseModel):
    current: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=128)


class FilePathRequest(BaseModel):
    """Relativer Pfad im Instanz-Ordner (Datei-Browser)."""
    path: str = Field(min_length=1, max_length=512)


class FileContentRequest(FilePathRequest):
    content: str = Field(default="", max_length=1 * 1024 * 1024)


class FileRenameRequest(BaseModel):
    from_path: str = Field(min_length=1, max_length=512)
    to_path: str = Field(min_length=1, max_length=512)


class DatapackNameRequest(BaseModel):
    """Datapack-Dateiname (nur .zip)."""
    name: str = Field(min_length=1, max_length=128)


class GameruleSetRequest(BaseModel):
    """Gamerule setzen (name aus kuratierter Liste, value bool/int)."""
    name: str = Field(min_length=1, max_length=64)
    value: bool | int | str | None = None


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Version + Update-Check (GitHub Releases, gecacht) für den Header-Chip
# ---------------------------------------------------------------------------

_GITHUB_RELEASES_URL = "https://api.github.com/repos/Chronixx4/minedocker/releases/latest"
_RELEASE_CHECK_TTL = 1800.0  # 30 min: GitHub-Rate-Limit schonen (60/h pro IP)
_release_cache: dict = {"checked": 0.0, "data": None}


def _version_tuple(text: str):
    """'v1.7.4' → (1, 7, 4); nicht numerische Versionen (dev, Commit-SHA)
    ergeben None — dann lieber keinen Update-Hinweis als eine Falschwarnung."""
    text = str(text).strip().lstrip("vV")
    if not text or not text[0].isdigit():
        return None
    parts = []
    for chunk in text.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            return None
        parts.append(int(digits))
    return tuple(parts) or None


def _release_is_newer(tag: str, current: str) -> bool:
    tag_v, cur_v = _version_tuple(tag), _version_tuple(current)
    if not tag_v or not cur_v:
        return False
    return tag_v > cur_v


async def _update_check() -> dict | None:
    """Letzte GitHub-Release-Version abrufen; mit Cache, wirft nie."""
    now = time.monotonic()
    if now - _release_cache["checked"] < _RELEASE_CHECK_TTL:
        return _release_cache["data"]  # auch None-Fehlversuche werden gecacht
    data = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _GITHUB_RELEASES_URL,
                headers={"Accept": "application/vnd.github+json"})
        if resp.status_code == 200:
            body = resp.json()
            tag = str(body.get("tag_name") or "")
            data = {
                "latest": tag,
                "url": body.get("html_url"),
                "available": _release_is_newer(tag, settings.app_version),
            }
    except Exception:
        data = None  # GitHub nicht erreichbar → kein Update-Hinweis
    _release_cache.update(checked=now, data=data)
    return data


@api.get("/meta")
async def app_meta():
    """Versionsinfo + Update-Check für das Header-Chip."""
    return {"version": settings.app_version, "update": await _update_check()}


# ---------------------------------------------------------------------------
# Auth: Setup, Login/Logout, Session, Benutzerverwaltung (Login & Rollen)
# ---------------------------------------------------------------------------

def _session_cookie_args() -> dict:
    """Cookie-Attribute zentral (HttpOnly + SameSite=Strict; CORS bleibt
    allow_credentials=False, das Frontend ist same-origin)."""
    return {"key": auth_mod.SESSION_COOKIE, "max_age": auth_mod.SESSION_TTL,
            "path": "/", "httponly": True, "samesite": "strict"}


def _issue_session(resp: JSONResponse, username: str, role: str) -> None:
    resp.set_cookie(value=auth_mod.create_token(username, role),
                    secure=False, **_session_cookie_args())


def _api_key_or_setup_open(request: Request) -> None:
    """Setup absichern, wenn DASHBOARD_API_KEY gesetzt ist: Der Key-Halter
    gilt als vertrauenswürdig, anonyme Anfragen werden abgelehnt (fail-closed
    — ohne Key-Schutz darf der erste Besucher den Admin anlegen, Homelab-Annahme)."""
    if settings.api_key and request.headers.get("X-API-Key") != settings.api_key:
        raise HTTPException(status_code=401,
                            detail="Setup erfordert den gültigen API-Key")


@app.get("/api/auth/setup-available")
async def auth_setup_available():
    return {"available": auth_mod.setup_available()}


@app.post("/api/auth/setup")
async def auth_setup(request: Request, req: AuthSetupRequest):
    """Ersten Benutzer (Admin) anlegen — nur solange users.json leer ist;
    sonst 409. Bei gesetztem DASHBOARD_API_KEY ist der Key Pflicht."""
    _api_key_or_setup_open(request)
    if not auth_mod.setup_available():
        raise HTTPException(status_code=409,
                            detail="Setup bereits abgeschlossen — es existiert "
                                   "bereits ein Benutzer")
    user = auth_mod.create_user(req.username, req.password, "admin")
    logger.info("Erster Admin angelegt: %s (Setup)", user["username"])
    resp = JSONResponse(status_code=201,
                        content={"username": user["username"], "role": user["role"]})
    _issue_session(resp, user["username"], user["role"])
    return resp


@app.post("/api/auth/login")
async def auth_login(request: Request, req: AuthLoginRequest):
    """Login mit Benutzername/Passwort → Set-Cookie (HMAC-Session).
    Lockout (in-memory) + konstanter Delay bei Fehlversuchen."""
    username = (req.username or "").strip().lower()
    if auth_mod.login_locked(username):
        await auth_mod.login_delay()
        raise HTTPException(status_code=429,
                            detail="Zu viele Fehlversuche — bitte 5 Minuten warten")
    user = auth_mod.find_user(username)
    password_ok = user is not None and auth_mod.verify_password(
        req.password or "", user.get("salt") or "", user.get("scrypt_hash") or "")
    if user is None or not password_ok:
        remaining = auth_mod.register_login_fail(username)
        await auth_mod.login_delay()
        if remaining:
            raise HTTPException(status_code=429,
                                detail="Zu viele Fehlversuche — Account für "
                                       "5 Minuten gesperrt")
        raise HTTPException(status_code=401,
                            detail="Benutzername oder Passwort falsch")
    auth_mod.clear_login_fails(username)
    resp = JSONResponse(content={"username": user["username"],
                                 "role": user["role"]})
    _issue_session(resp, user["username"], user["role"])
    logger.info("Login: %s (%s)", user["username"], user["role"])
    return resp


@app.post("/api/auth/logout")
async def auth_logout():
    """Abmelden: Cookie löschen (Server-seitig bleibt die zustandslose
    Session bis zum Ablauf formal gültig — dokumentierte Homelab-Vereinfachung)."""
    resp = JSONResponse(content={"logged_out": True})
    resp.delete_cookie(key=auth_mod.SESSION_COOKIE, path="/",
                       httponly=True, samesite="strict")
    return resp


@app.get("/api/auth/me")
async def auth_me(request: Request,
                  x_api_key: str | None = Header(default=None)):
    """Aktuelle Session für das Frontend: {username, role} oder
    {authenticated: false} plus Hinweise für Login-/Setup-Overlay.
    Ein gültiger API-Key zählt als angemeldeter Admin (Skripte/Bestands-
    Frontends mit gespeichertem Key)."""
    from .config import settings  # lazy (gleiche Datei, aber klarer)
    if settings.api_key and x_api_key == settings.api_key:
        return {"authenticated": True, "username": "(api-key)", "role": "admin",
                "login_active": auth_mod.login_active(),
                "setup_available": auth_mod.setup_available(),
                "api_key_required": True}
    session = auth_mod.current_session(request)
    if session:
        return {"authenticated": True, "username": session.get("u"),
                "role": session.get("r"), "login_active": auth_mod.login_active(),
                "setup_available": False, "api_key_required": bool(settings.api_key)}
    return {"authenticated": False, "username": None, "role": None,
            "login_active": auth_mod.login_active(),
            "setup_available": auth_mod.setup_available(),
            "api_key_required": bool(settings.api_key)}


@app.get("/api/auth/users")
async def auth_users_list(request: Request):
    auth_mod.require_admin(request)
    users = [{k: u.get(k) for k in ("username", "role", "created")}
             for u in auth_mod.list_users()]
    return {"users": users}


@app.post("/api/auth/users", status_code=201)
async def auth_users_create(request: Request, req: AuthUserCreateRequest):
    admin = auth_mod.require_admin(request)
    user = auth_mod.create_user(req.username, req.password, req.role)
    logger.info("Benutzer angelegt: %s (%s) durch %s",
                user["username"], user["role"], admin.get("u"))
    return user


@app.patch("/api/auth/users/{username}")
async def auth_users_patch(request: Request, username: str,
                           req: AuthUserPatchRequest):
    auth_mod.require_admin(request)
    result = auth_mod.update_user(username, role=req.role, password=req.password)
    logger.info("Benutzer geändert: %s (%s)", username,
                ", ".join(result["changed"]))
    return result


@app.delete("/api/auth/users/{username}")
async def auth_users_delete(request: Request, username: str):
    session = auth_mod.require_admin(request)
    result = auth_mod.delete_user(username, session.get("u") or "")
    logger.info("Benutzer gelöscht: %s", username)
    return result


@app.post("/api/auth/change-password")
async def auth_change_password(request: Request, req: AuthChangePasswordRequest):
    """Eigenes Passwort ändern (Admin und Viewer) — aktuelles Passwort Pflicht.
    Die Änderung invalidiert alle ausgestellten Sessions; der Aufrufer bekommt
    direkt einen frischen Token (bleibt eingeloggt)."""
    session = auth_mod.require_session(request)
    result = auth_mod.change_own_password(session["u"], req.current, req.password)
    logger.info("Passwort geändert: %s", result["username"])
    user = auth_mod.find_user(result["username"])
    resp = JSONResponse(content=result)
    if user:
        _issue_session(resp, str(user["username"]), str(user.get("role") or "viewer"))
    return resp


@api.get("/settings")
async def get_settings():
    return {
        "mc_version": current_game_version(),
        "mod_loader": settings.mod_loader,
        "loader_version": settings.loader_version or None,
        "mc_host": settings.mc_host,
        "mc_port": settings.mc_port,
        "mods_dir": str(settings.mods_dir),
        "auth_required": bool(settings.api_key),
        # RCON-Konsole: Modus + erlaubte Befehle (für die UI-Hinweise)
        "rcon_console_mode": settings.rcon_console_mode,
        "rcon_console_whitelist": sorted(_CONSOLE_ALLOWED | {
            w.lower() for w in settings.rcon_console_whitelist}),
    }


# RCON-Konsole: Befehle, die im Whitelist-Modus erlaubt sind (Serverinfra-
# Befehle wie stop/save-off bleiben immer außen vor, außer im freien Modus)
_CONSOLE_ALLOWED = frozenset({
    "op", "deop", "kick", "ban", "pardon", "banlist", "list", "whitelist",
    "say", "me", "msg", "tell", "w", "seed", "gamerule", "difficulty",
    "weather", "time",
})


def _normalize_console_command(raw: str) -> str:
    """Konsolenbefehl säubern: führenden Slash entfernen, Steuerzeichen
    und Rand-Whitespace ablegen."""
    cmd = (raw or "").strip().lstrip("/")
    cmd = "".join(ch for ch in cmd if ord(ch) >= 32)
    cmd = cmd.strip()
    if not cmd:
        raise HTTPException(status_code=400, detail="Leerer Befehl")
    if len(cmd) > 256:
        raise HTTPException(status_code=400, detail="Befehl zu lang (max. 256 Zeichen)")
    return cmd


def _check_console_command(cmd: str) -> str:
    """Whitelist-Modus: nur freigegebene Befehle; freier Modus: alles."""
    if settings.rcon_console_mode == "free":
        return cmd
    first = cmd.split(None, 1)[0].lower() if cmd.split() else ""
    allowed = _CONSOLE_ALLOWED | {w.lower() for w in settings.rcon_console_whitelist}
    if first not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Befehl '{first[:32]}' ist nicht freigegeben (Whitelist-Modus). "
                   f"Erlaubt sind: {', '.join(sorted(allowed))} — "
                   "oder RCON_CONSOLE_MODE=free setzen")
    return cmd


@api.get("/status")
async def status():
    """Ressourcen aller laufenden Minecraft-Container (Instanzen + ggf.
    legacy 'minecraft'-Container) über die Docker-Engine-API."""
    resources = await asyncio.to_thread(runtime.docker_resources)
    return {"resources": resources}


@api.get("/mods")
async def list_mods():
    try:
        settings.mods_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"mods-Ordner nicht lesbar: {exc}")
    mods = await asyncio.to_thread(instances.list_mods_in, settings.mods_dir)
    return {"mods": mods, "directory": str(settings.mods_dir)}


@api.delete("/mods/{filename}")
async def delete_mod(filename: str):
    path = safe_mods_path(settings.mods_dir, filename)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Mod nicht gefunden")
    try:
        path.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Löschen fehlgeschlagen: {exc}")
    updates_mod.invalidate_installed_cache(None)
    logger.info("Mod gelöscht: %s", filename)
    return {"deleted": filename}


@api.patch("/mods/{filename}")
async def toggle_mod(filename: str, req: ToggleModRequest):
    """Mod am Hauptspeicherort aktivieren/deaktivieren (Umbenennen)."""
    result = instances.toggle_mod_in(settings.mods_dir, filename, req.enabled)
    updates_mod.invalidate_installed_cache(None)
    logger.info("Mod %s: %s → %s",
                "aktiviert" if req.enabled else "deaktiviert", filename, result["filename"])
    return result


# Erlaubte Sortierungen für die Mod-Suche (beide Quellen)
_SORT_KEYS = ("relevance", "downloads", "updated", "newest")


def _parse_search_filters(loader: str | None,
                          game_version: str | None) -> tuple[str | None, str | None]:
    """Filter-Parameter der Mod-Suche auswerten.

    loader=None/"" → Standard-Loader, "any" → kein Loader-Filter.
    game_version=None/"" → aktuelle MC-Version, "any" → keine Versionsfilterung."""
    loader = (loader or "").strip().lower()
    if loader == "any":
        loader = None
    else:
        loader = loader or settings.mod_loader
        if loader not in ALLOWED_LOADERS:
            raise HTTPException(status_code=400,
                                detail=f"Loader muss einer von {', '.join(ALLOWED_LOADERS)} sein")
    if game_version is None or not game_version.strip():
        return loader, current_game_version()
    game_version = game_version.strip()
    if game_version.lower() == "any":
        return loader, None
    return loader, validate_identifier(game_version, "Minecraft-Version")


def _parse_sort(sort: str | None) -> str:
    sort = (sort or "").strip().lower() or "relevance"
    if sort not in _SORT_KEYS:
        raise HTTPException(status_code=400,
                            detail=f"Sortierung muss einer von {', '.join(_SORT_KEYS)} sein")
    return sort


async def _mark_installed(data: object, instance_id: str | None, source: str) -> None:
    """Markiert Treffer der Mod-Suche mit 'installed', wenn der Mod bereits
    in der Ziel-Instanz liegt (per Datei-Hash identifiziert — Modrinth-
    Projekt-ID bzw. CurseForge-Mod-ID). Fehler schlagen die Suche nicht fehl."""
    if not instance_id or not isinstance(data, dict) or not data.get("hits"):
        return
    instance_id = validate_identifier(instance_id, "Instanz-ID")
    try:
        ids = await updates_mod.installed_project_ids(instance_id)
    except Exception as exc:
        logger.warning("Installiert-Markierung nicht möglich (%s): %s",
                       instance_id, exc)
        return
    installed = ids.get("modrinth" if source == "modrinth" else "curseforge") or set()
    if not installed:
        return
    for hit in data.get("hits") or []:
        if isinstance(hit, dict):
            hit["installed"] = str(hit.get("project_id") or "") in installed


@api.get("/modrinth/search")
async def modrinth_search(q: str = "", offset: int = 0,
                          loader: str | None = None,
                          game_version: str | None = None,
                          sort: str | None = None,
                          environment: str | None = None,
                          instance_id: str | None = None):
    loader, game_version = _parse_search_filters(loader, game_version)
    sort = _parse_sort(sort)
    query = (q or "").strip()[:100]
    offset = max(0, min(offset, 1000))
    data = await modrinth.search_mods(query, loader, game_version,
                                      sort=sort, offset=offset,
                                      environment=(environment or "").strip().lower() or None)
    await _mark_installed(data, instance_id, "modrinth")
    return data


async def _start_mod_download(req: DownloadRequest, resolver, provider: str,
                              source: str, verify_url: bool = False):
    """Gemeinsame Logik für Einzel-Mod-Downloads (Modrinth & CurseForge):
    Ziel bestimmen (Hauptserver/Instanz) → Datei auflösen → 409 bei Konflikt →
    Pflicht-Abhängigkeiten auflösen (best effort) → Download-Job starten.
    resolver liefert (filename, url, size, sha1, cf_dep_ids)."""
    if req.instance_id:
        instance = instances.get_instance(req.instance_id)
        loader = instance["loader"]
        game_version = instance["game_version"]
        target_dir = instances.mods_dir(instance["id"])
        target_label = f"Instanz '{instance['name']}'"
    else:
        loader = settings.mod_loader
        game_version = current_game_version()
        target_dir = settings.mods_dir
        target_label = "Hauptserver"
    try:
        filename, url, size, sha1, cf_dep_ids = await resolver(
            req.project_id, req.version_id,
            loader=loader, game_version=game_version)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"{provider}-Auflösung fehlgeschlagen")
        raise HTTPException(status_code=502, detail=f"{provider} nicht erreichbar: {exc}")

    dest = safe_mods_path(target_dir, filename)
    if dest.exists() and not req.overwrite:
        return JSONResponse(status_code=409, content={
            "detail": f"'{filename}' existiert bereits im mods-Ordner ({target_label})",
            "filename": filename,
        })
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"mods-Ordner nicht schreibbar: {exc}")

    # Pflicht-Abhängigkeiten automatisch mitinstallieren (nur Instanz-Ziel):
    # Modrinth per Versions-Metadaten, CurseForge per Relations-Feld der
    # Datei (liefert CF nicht bei allen Dateien). Best effort: Scheitern
    # blockiert die Hauptinstallation nicht, bereits installierte Projekte
    # werden gefiltert.
    deps: list = []
    skipped: list = []
    if req.instance_id and req.with_dependencies:
        try:
            installed = set((await updates_mod.installed_project_ids(
                req.instance_id))["modrinth" if source == "modrinth" else "curseforge"])
            installed.add(str(req.project_id))
            if source == "modrinth":
                bundle = await modrinth.dependency_files(
                    req.project_id, req.version_id, loader, game_version, installed)
            else:
                bundle = await curseforge.dependency_files(
                    cf_dep_ids or [], loader, game_version, installed)
            for entry in bundle.get("files") or []:
                if safe_mods_path(target_dir, entry["filename"]).exists():
                    skipped.append({"filename": entry["filename"],
                                    "project_id": entry.get("project_id"),
                                    "reason": "Datei existiert bereits"})
                    continue
                entry["kind"] = "dep"
                deps.append(entry)
            skipped.extend(bundle.get("skipped") or [])
        except Exception as exc:
            logger.warning("Abhängigkeiten nicht auflösbar (%s): %s",
                           req.project_id, exc)

    entries = [{"filename": filename, "url": url, "size": size, "sha1": sha1,
                "kind": "main", "project_id": str(req.project_id)}]
    entries.extend(deps)
    total = size + sum(int(d.get("size") or 0) for d in deps)
    job = modrinth.create_job(filename, total, kind="mod", source=source,
                              instance_id=req.instance_id)
    job["bundle"] = entries
    if deps:
        job["dependencies"] = [{"filename": d["filename"],
                                "project_id": d.get("project_id")} for d in deps]
    if skipped:
        job["skipped"] = skipped
    modrinth.persist_job(job)
    if deps:
        modrinth.track_task(asyncio.create_task(
            modrinth.run_bundle_job(job, target_dir, verify_url=verify_url)))
    else:
        modrinth.track_task(asyncio.create_task(
            modrinth.run_download_job(job, url, dest, sha1=sha1, verify_url=verify_url)))
    logger.info("%s-Download gestartet: %s (%d Bytes, %d Abhängigkeit(en)) → %s",
                provider, filename, size, len(deps), target_label)
    return {"job_id": job["id"], "filename": filename, "size": size,
            "target": {"type": "instance" if req.instance_id else "main",
                       "instance_id": req.instance_id},
            "dependencies": job.get("dependencies") or [],
            "skipped": job.get("skipped") or []}


@api.post("/modrinth/download")
async def modrinth_download(req: DownloadRequest):
    async def resolver(project_id, version_id, loader, game_version):
        filename, url, size = await modrinth.resolve_download(
            project_id, version_id, loader=loader, game_version=game_version)
        return filename, url, size, None, None

    return await _start_mod_download(req, resolver, "Modrinth", "modrinth")


@api.get("/modrinth/jobs/{job_id}")
async def modrinth_job(job_id: str):
    job = modrinth.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    return job


@api.get("/jobs/{job_id}")
async def any_job(job_id: str):
    """Generischer Job-Endpunkt (Mod-Downloads UND Modpack-Installationen)."""
    job = modrinth.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    return job


# ---------------------------------------------------------------------------
# CurseForge (Mod-/Modpack-Suche + Direktdownload; benötigt CF_API_KEY)
# ---------------------------------------------------------------------------

@api.get("/curseforge/search")
async def curseforge_search(q: str = "", offset: int = 0,
                            loader: str | None = None,
                            game_version: str | None = None,
                            sort: str | None = None,
                            instance_id: str | None = None):
    loader, game_version = _parse_search_filters(loader, game_version)
    sort = _parse_sort(sort)
    query = (q or "").strip()[:100]
    data = await curseforge.search_mods(query, loader, game_version,
                                        sort=sort, offset=offset)
    await _mark_installed(data, instance_id, "curseforge")
    return data


@api.post("/curseforge/download")
async def curseforge_download(req: DownloadRequest):
    """Lädt eine Einzel-Mod von CurseForge (Hauptserver oder Instanz);
    Pflicht-Abhängigkeiten werden best effort mitinstalliert."""
    async def resolver(project_id, version_id, loader, game_version):
        return await curseforge.resolve_download_full(
            project_id, version_id, loader, game_version)

    return await _start_mod_download(req, resolver, "CurseForge", "curseforge",
                                     verify_url=True)


# ---------------------------------------------------------------------------
# Katalog: Spielversionen + Loader (für die Server-Erstellung)
# ---------------------------------------------------------------------------

@api.get("/catalog/mc-versions")
async def catalog_mc_versions():
    return await catalog.mc_versions()


@api.get("/catalog/loaders")
async def catalog_loaders(game_version: str = Query(..., min_length=1, max_length=32)):
    return await catalog.loaders(game_version.strip())


# ---------------------------------------------------------------------------
# Multi-Server-Verwaltung
# ---------------------------------------------------------------------------

def _instance_ping_host(instance: dict) -> tuple:
    """(Host, Port) für SLP-Ping einer Instanz aus Sicht des Dashboards."""
    if Path("/.dockerenv").exists():
        # Im Docker-Netz ist die Instanz unter ihrem Container-Namen erreichbar
        return runtime.container_name(instance["id"]), 25565
    return "127.0.0.1", int(instance["port"])


async def _stream_upload(file: UploadFile, dest: Path, max_bytes: int,
                         label: str = "4 GiB") -> int:
    """Streamend in eine Datei schreiben (Deckel gegen Speicher-Füllung);
    schließt den Upload und wirft 413 bei Überschreitung."""
    written = 0
    try:
        with open(dest, "wb") as fh:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=413,
                                        detail=f"Datei zu groß (max {label})")
                fh.write(chunk)
    finally:
        await file.close()
    return written


async def _read_capped(file: UploadFile, max_bytes: int,
                       label: str) -> bytes:
    """Datei in den Speicher lesen (Deckel gegen Speicher-Füllung);
    schließt den Upload und wirft 413 bei Überschreitung."""
    buf = bytearray()
    try:
        while chunk := await file.read(1024 * 1024):
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise HTTPException(status_code=413,
                                    detail=f"Datei zu groß (max {label})")
    finally:
        await file.close()
    return bytes(buf)


@api.get("/instances")
async def list_instances():
    result = []
    for instance in instances.list_instances():
        status = await asyncio.to_thread(runtime.container_status, instance)
        entry = dict(instance)
        entry["container"] = {"running": status["running"], "state": status["container"]}
        if status.get("started_at"):
            entry["container"]["started_at"] = status["started_at"]
        if status.get("error"):
            entry["container"]["error"] = status["error"]
        result.append(entry)
    return {"instances": result, "directory": str(settings.instances_dir)}


@api.get("/instances/live")
async def live_instances():
    """SLP-Ping aller laufenden Instanzen (parallel, je 2 s Timeout).
    Liefert Spielerzahl, MOTD und Version für die Übersicht; gestoppte
    Instanzen fehlen. Wirft nicht — Pings liefern immer ein Objekt."""
    running = []
    for instance in instances.list_instances():
        status = await asyncio.to_thread(runtime.container_status, instance)
        if status["running"]:
            running.append(instance)

    async def ping_one(instance: dict) -> dict:
        ping_host, ping_port = _instance_ping_host(instance)
        ping = await asyncio.to_thread(server_status, ping_host, ping_port, 2.0)
        return {
            "id": instance["id"],
            "name": instance["name"],
            "port": instance["port"],
            "ping": ping,
        }

    live = list(await asyncio.gather(*(ping_one(i) for i in running))) \
        if running else []
    return {"live": live}


@api.post("/instances", status_code=201)
async def create_instance(req: CreateInstanceRequest):
    instance = instances.create_instance(
        req.name, req.loader, req.game_version,
        loader_version=req.loader_version, port=req.port,
        memory=req.memory, accept_eula=req.accept_eula, tags=req.tags)
    logger.info("Instanz erstellt: %s (%s %s, Port %s)",
                instance["name"], instance["loader"],
                instance["game_version"], instance["port"])
    return instance


@api.post("/instances/import", status_code=201)
async def import_instance(
        name: str = Form(...),
        loader: str = Form(...),
        game_version: str = Form(...),
        loader_version: str | None = Form(None),
        port: int | None = Form(None),
        memory: str | None = Form(None),
        accept_eula: bool = Form(False),
        file: UploadFile = File(...)):
    """Bindet einen bestehenden Server-Ordner (.zip/.tar.gz mit Welt, Mods,
    Configs) als neue Instanz ein. server.properties wird auf die Container-
    Erfordernisse angepasst (Port/RCON), EULA wird erzwungen akzeptiert."""
    original = worlds.validate_archive_filename(file.filename or "")
    staging_path = worlds.staging_dir() / f"import_{uuid.uuid4().hex[:8]}_{original}"
    try:
        await _stream_upload(file, staging_path, packs._MAX_PACK_BYTES)
        instance = await asyncio.to_thread(
            worlds.import_server, staging_path, original,
            name=name, loader=loader, game_version=game_version,
            loader_version=(loader_version or "").strip() or None,
            port=port, memory=memory or None, accept_eula=accept_eula)
    finally:
        staging_path.unlink(missing_ok=True)
    logger.info("Server importiert: %s (%s %s, Port %s)",
                instance["name"], instance["loader"],
                instance["game_version"], instance["port"])
    return instance


@api.post("/instances/from-pack", status_code=201)
async def instance_from_pack(req: CreateFromPackRequest):
    """Erstellt eine neue Server-Instanz direkt aus einem Modpack:
    MC-Version/Loader werden aus dem Pack übernommen, das Pack anschließend
    in die neue Instanz installiert (Job in der Antwort)."""
    try:
        result = await packs.create_server_from_pack(
            project_id=req.project_id, source=req.source,
            version_id=req.version_id, file_id=req.file_id,
            name=(req.name or "").strip() or None, memory=req.memory,
            port=req.port, accept_eula=req.accept_eula,
            prefer_server_pack=req.prefer_server_pack)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Server aus Modpack konnte nicht erstellt werden")
        raise HTTPException(status_code=502,
                            detail=f"Modpack-Anbieter nicht erreichbar: {exc}")
    logger.info("Server aus Modpack erstellt: %s (Port %s, %s %s)",
                result["instance"]["name"], result["instance"]["port"],
                result["instance"]["loader"], result["instance"]["game_version"])
    return {"instance": result["instance"], "job": result["job"]}


@api.post("/instances/from-pack-upload", status_code=201)
async def instance_from_pack_upload(name: str = Form(""),
                                    memory: str = Form(""),
                                    port: int | None = Form(None),
                                    accept_eula: bool = Form(False),
                                    file: UploadFile = File(...)):
    """Erstellt aus einem hochgeladenen Modpack-Archiv (.mrpack/.zip) direkt
    eine neue Server-Instanz: Loader/MC-Version werden aus dem Index gelesen."""
    filename = instances.validate_pack_filename(file.filename or "")
    archive = None
    try:
        archive = packs.staging_dir() / f"upload_{uuid.uuid4().hex[:8]}_{filename}"
        written = 0
        with open(archive, "wb") as fh:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > packs._MAX_PACK_BYTES:
                    raise HTTPException(status_code=413,
                                        detail="Modpack zu groß (max 4 GiB)")
                fh.write(chunk)
    except HTTPException:
        if archive is not None:
            archive.unlink(missing_ok=True)
        raise
    except OSError as exc:
        if archive is not None:
            archive.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Upload fehlgeschlagen: {exc}")
    finally:
        await file.close()
    try:
        result = await packs.create_server_from_upload(
            archive, filename, name=(name or "").strip() or None,
            memory=memory or None, port=port, accept_eula=accept_eula)
    except HTTPException:
        archive.unlink(missing_ok=True)
        raise
    except Exception as exc:
        archive.unlink(missing_ok=True)
        logger.exception("Server aus Upload konnte nicht erstellt werden")
        raise HTTPException(status_code=500,
                            detail=f"Erstellung fehlgeschlagen: {exc}")
    logger.info("Server aus Upload erstellt: %s (Port %s, %s %s)",
                result["instance"]["name"], result["instance"]["port"],
                result["instance"]["loader"], result["instance"]["game_version"])
    return {"instance": result["instance"], "job": result["job"]}


@api.get("/instances/{instance_id}")
async def instance_detail(instance_id: str):
    instance = instances.get_instance(instance_id)
    ping_host, ping_port = _instance_ping_host(instance)
    # Docker-Status, Ping, Mod-Scan und Disk-Scan parallel (statt seriell) —
    # die Antwort kommt so nach der langsamsten Einzelabfrage, nicht nach
    # der Summe aller; jeder Schritt läuft im Worker-Thread, damit der
    # Event-Loop nicht blockiert (sonst hängt das ganze Panel kurz).
    status, ping, mods, disk = await asyncio.gather(
        asyncio.to_thread(runtime.container_status, instance),
        asyncio.to_thread(server_status, ping_host, ping_port, 1.0),
        asyncio.to_thread(instances.list_mods, instance_id),
        asyncio.to_thread(instances.disk_usage, instance_id),
    )
    detail = dict(instance)
    detail["container"] = {"running": status["running"], "state": status["container"]}
    if status.get("started_at"):
        detail["container"]["started_at"] = status["started_at"]
    if status.get("error"):
        detail["container"]["error"] = status["error"]
    detail["ping"] = ping
    detail["mods"] = mods
    detail["disk"] = disk
    return detail


@api.patch("/instances/{instance_id}")
async def instance_update(instance_id: str, req: UpdateInstanceRequest):
    """Instanz-Einstellungen ändern (Name/RAM/JVM-Flags/Tags/Port/Zeitplan);
    JVM-/Port-Änderungen wirken beim nächsten (Neu-)Start."""
    schedule = req.schedule
    body = {k: v for k, v in req.model_dump(exclude={"schedule"}).items()
            if v is not None}
    if not body and schedule is None:
        raise HTTPException(status_code=400,
                            detail="Keine Änderungen übergeben "
                                   "(name/memory/jvm_opts/use_aikar/tags/port/schedule)")
    result = {"instance": instances.get_instance(instance_id), "changed": []}
    if schedule is not None:
        result = instances.update_schedule(
            instance_id, schedule.model_dump(exclude_none=True))
        logger.info("Zeitplan aktualisiert: %s (%s)", instance_id,
                    ", ".join(result["changed"]))
    if body:
        # update_settings macht Docker-I/O (is_running) + Datei-Scans →
        # Worker-Thread, damit der Event-Loop nicht blockiert (wie bei
        # start/stop/clone üblich)
        result = await asyncio.to_thread(instances.update_settings,
                                         instance_id, **body)
        logger.info("Instanz aktualisiert: %s (%s)", instance_id,
                    ", ".join(result["changed"]))
    return result["instance"]


@api.post("/instances/{instance_id}/clone", status_code=201)
async def instance_clone(instance_id: str, req: CloneInstanceRequest):
    """Klont eine gestoppte Instanz als Vorlage (Welt, Mods, Configs)."""
    try:
        instance = await asyncio.to_thread(
            instances.clone_instance, instance_id, (req.name or "").strip() or None)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Klonen fehlgeschlagen")
        raise HTTPException(status_code=500, detail=f"Klonen fehlgeschlagen: {exc}")
    logger.info("Instanz geklont: %s → %s (Port %s)",
                instance_id, instance["name"], instance["port"])
    return instance


@api.post("/instances/{instance_id}/start")
async def instance_start(instance_id: str):
    instance = instances.get_instance(instance_id)
    container = await asyncio.to_thread(runtime.container_status, instance)
    if container.get("running"):
        instances.set_status(instance_id, "running", None)
        return {"status": "running", "detail": "Läuft bereits"}
    instances.set_status(instance_id, "starting", None)
    try:
        await asyncio.to_thread(runtime.start_instance, instance)
    except RuntimeError as exc:
        instances.set_status(instance_id, "error", str(exc))
        raise HTTPException(status_code=503, detail=str(exc))
    instances.set_status(instance_id, "running", None)
    return {"status": "running"}


@api.post("/instances/{instance_id}/stop")
async def instance_stop(instance_id: str):
    instance = instances.get_instance(instance_id)
    watchdog_mod.expect_stop(instance_id)  # Watchdog: kein Crash-Alert
    try:
        await asyncio.to_thread(runtime.stop_instance, instance)
    except RuntimeError as exc:
        instances.set_status(instance_id, "error", str(exc))
        raise HTTPException(status_code=503, detail=str(exc))
    instances.set_status(instance_id, "stopped", None)
    return {"status": "stopped"}


@api.post("/instances/{instance_id}/restart")
async def instance_restart(instance_id: str):
    """Stoppt und startet den Instanz-Container (sauberer Neustart)."""
    instance = instances.get_instance(instance_id)
    instances.set_status(instance_id, "starting", None)
    watchdog_mod.expect_stop(instance_id)  # Watchdog: Stop gehört zum Neustart
    try:
        await asyncio.to_thread(runtime.stop_instance, instance)
    except RuntimeError as exc:
        instances.set_status(instance_id, "error", str(exc))
        raise HTTPException(status_code=503, detail=f"Stop fehlgeschlagen: {exc}")
    try:
        await asyncio.to_thread(runtime.start_instance, instance)
    except RuntimeError as exc:
        instances.set_status(instance_id, "error", str(exc))
        raise HTTPException(status_code=503, detail=f"Start fehlgeschlagen: {exc}")
    instances.set_status(instance_id, "running", None)
    logger.info("Instanz neu gestartet: %s", instance_id)
    return {"status": "running"}


@api.get("/instances/{instance_id}/logs")
async def instance_logs(instance_id: str, tail: int = Query(100, ge=1, le=500)):
    instance = instances.get_instance(instance_id)
    try:
        lines = await asyncio.to_thread(runtime.logs, instance, tail)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"logs": lines}


@api.get("/instances/{instance_id}/logs/stream")
async def instance_log_stream(instance_id: str, tail: int = Query(100, ge=1, le=500)):
    """Live-Logs als Server-Sent Events: erst der tail-Rückstand, dann neue
    Zeilen in Echtzeit. Endet, wenn der Container stoppt; Keepalive-Kommentare
    halten die Verbindung warm."""
    instance = instances.get_instance(instance_id)

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        loop = asyncio.get_running_loop()
        stop = threading.Event()

        def _enqueue(item):
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                pass  # Rückstand verwerfen, statt unbegrenzt zu wachsen

        def reader():
            try:
                for line in runtime.follow_logs(instance, tail):
                    if stop.is_set():
                        return
                    loop.call_soon_threadsafe(_enqueue, line)
            except Exception:
                pass  # Stream-/Docker-Fehler: Verbindung sauber beenden
            finally:
                loop.call_soon_threadsafe(_enqueue, None)

        thread = threading.Thread(target=reader, daemon=True,
                                  name=f"logstream-{instance_id}")
        thread.start()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": keepalive\n\n"  # SSE-Kommentar: kein Event
                    continue
                if item is None:
                    break  # Stream-Ende (Container gestoppt)
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            stop.set()

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@api.delete("/instances/{instance_id}")
async def instance_delete(instance_id: str, force: bool = False):
    # Watchdog: ein erzwungenes Entfernen darf nicht als Crash gemeldet werden
    watchdog_mod.expect_stop(instance_id)
    result = instances.delete_instance(instance_id, force=force)
    logger.info("Instanz gelöscht: %s", instance_id)
    return result


@api.get("/instances/{instance_id}/mods")
async def instance_mods(instance_id: str):
    instances.get_instance(instance_id)  # 404 wenn unbekannt
    return {"mods": await asyncio.to_thread(instances.list_mods, instance_id)}


@api.delete("/instances/{instance_id}/mods/{filename}")
async def instance_delete_mod(instance_id: str, filename: str):
    instances.get_instance(instance_id)
    deleted = instances.delete_mod(instance_id, filename)
    instances.invalidate_disk_cache(instance_id)
    updates_mod.invalidate_installed_cache(instance_id)
    return {"deleted": deleted}


@api.patch("/instances/{instance_id}/mods/{filename}")
async def instance_toggle_mod(instance_id: str, filename: str, req: ToggleModRequest):
    """Mod einer Instanz aktivieren/deaktivieren (Umbenennen .jar <-> .jar.disabled)."""
    instances.get_instance(instance_id)
    result = instances.toggle_mod(instance_id, filename, req.enabled)
    instances.invalidate_disk_cache(instance_id)
    updates_mod.invalidate_installed_cache(instance_id)
    logger.info("Instanz %s: Mod %s → %s", instance_id, filename, result["filename"])
    return result


# ---------------------------------------------------------------------------
# Server-Einstellungen (server.properties) + Spieler-Verwaltung (RCON)
# ---------------------------------------------------------------------------

@api.get("/instances/{instance_id}/config")
async def instance_config_get(instance_id: str):
    instances.get_instance(instance_id)
    return {"properties": instances.read_server_properties(instance_id),
            "schema": instances.properties_schema(),
            "file": "server.properties"}


@api.post("/instances/{instance_id}/config")
async def instance_config_post(instance_id: str, req: ConfigRequest):
    """Schreibt server.properties neu. Änderungen werden erst beim nächsten
    (Neu-)Start des Servers wirksam — daher der Hinweis restart_required."""
    instance = instances.get_instance(instance_id)
    saved = instances.write_server_properties(
        instance_id, [p.model_dump() for p in req.properties])
    running = await asyncio.to_thread(runtime.is_running, instance)
    logger.info("server.properties gespeichert: %s (%d Eigenschaften, läuft=%s)",
                instance_id, saved, running)
    return {"saved": saved, "restart_required": running}


def _rcon_command(req: RconRequest) -> str:
    """RCON-Kommando aus der Aktion bauen (Whitelist im Request-Modell)."""
    if req.action == "list":
        return "list"
    if req.action == "banlist":
        return "banlist players"
    if not req.target:
        raise HTTPException(status_code=400,
                            detail=f"Aktion '{req.action}' braucht einen Spielernamen")
    if req.action == "kick":
        return f"kick {req.target} {req.reason or 'Vom Dashboard gekickt'}"
    if req.action == "ban":
        return f"ban {req.target} {req.reason or 'Gebannt über das Dashboard'}"
    return f"{req.action} {req.target}"


async def _rcon_run(instance: dict, cmd: str) -> str:
    """RCON-Kommando ausführen (nur bei laufendem Server); wirft HTTPException."""
    if not await asyncio.to_thread(runtime.is_running, instance):
        raise HTTPException(status_code=409,
                            detail="Instanz läuft nicht — RCON braucht einen gestarteten Server")
    host, port = runtime.rcon_target(instance)
    try:
        return await asyncio.to_thread(
            rcon_mod.command, host, port,
            runtime.rcon_secret(instance), cmd)
    except rcon_mod.RconError as exc:
        raise HTTPException(status_code=503,
                            detail=f"RCON nicht erreichbar: {exc} — bestehende Server "
                                   f"einmal neu starten, damit RCON aktiviert wird")


@api.post("/instances/{instance_id}/rcon")
async def instance_rcon(instance_id: str, req: RconRequest):
    """Führt eine Spieler-Verwaltungsaktion aus (op/deop/kick/ban/pardon/banlist)."""
    instance = instances.get_instance(instance_id)
    cmd = _rcon_command(req)
    output = await _rcon_run(instance, cmd)
    logger.info("RCON %s: %s → %s", instance_id, cmd, output[:120])
    return {"command": cmd, "output": output}


@api.get("/instances/{instance_id}/players")
async def instance_players(instance_id: str):
    """Online-Spieler via RCON 'list' (Namen, Anzahl, Slots)."""
    instance = instances.get_instance(instance_id)
    output = await _rcon_run(instance, "list")
    parsed = rcon_mod.parse_list_output(output)
    if parsed is None:
        return {"online": None, "max": None, "names": [], "raw": output}
    return {"online": parsed["online"], "max": parsed["max"],
            "names": parsed["names"], "raw": output}


@api.post("/instances/{instance_id}/console")
async def instance_console(instance_id: str, req: ConsoleRequest):
    """Führt ein freies RCON-Kommando aus. Im Whitelist-Modus (Standard)
    sind nur freigegebene Befehle erlaubt; RCON_CONSOLE_MODE=free erlaubt
    beliebige Befehle."""
    instance = instances.get_instance(instance_id)
    cmd = _check_console_command(_normalize_console_command(req.command))
    output = await _rcon_run(instance, cmd)
    logger.info("RCON-Konsole %s: %s → %s", instance_id, cmd, output[:120])
    return {"command": cmd, "output": output}


# ---------------------------------------------------------------------------
# Whitelist (whitelist.json, auch offline editierbar)
# ---------------------------------------------------------------------------

@api.get("/instances/{instance_id}/whitelist")
async def instance_whitelist_get(instance_id: str):
    """whitelist.json der Instanz lesen (auch bei gestoppter Instanz)."""
    instance = instances.get_instance(instance_id)
    data = await asyncio.to_thread(whitelist_mod.read_whitelist, instance_id)
    online_mode = await asyncio.to_thread(whitelist_mod.read_online_mode, instance_id)
    running = await asyncio.to_thread(runtime.is_running, instance)
    return {**data, "online_mode": online_mode, "running": running}


@api.post("/instances/{instance_id}/whitelist")
async def instance_whitelist_post(instance_id: str, req: WhitelistRequest):
    """whitelist.json ersetzen (offline möglich). Läuft der Server, wird die
    Whitelist danach per RCON 'whitelist reload' best-effort neu geladen."""
    instance = instances.get_instance(instance_id)
    result = await whitelist_mod.write_whitelist(instance_id, req.entries)
    reload_info = await whitelist_mod.reload_on_server(instance)
    logger.info("Whitelist gespeichert: %s (%d Einträge, UUIDs ungelöst: %d)",
                instance_id, len(result["entries"]), len(result["unresolved"]))
    return {"entries": result["entries"], "unresolved": result["unresolved"],
            "reloaded": reload_info["reloaded"],
            "reload_error": reload_info["reload_error"]}


@api.post("/instances/{instance_id}/whitelist/reload")
async def instance_whitelist_reload(instance_id: str):
    """Lädt die Whitelist auf dem laufenden Server neu (RCON)."""
    instance = instances.get_instance(instance_id)
    info = await whitelist_mod.reload_on_server(instance)
    if info["reloaded"] is None:
        raise HTTPException(status_code=409,
                            detail="Instanz läuft nicht — die Whitelist wird "
                                   "beim nächsten Start eingelesen")
    if info["reloaded"] is False:
        raise HTTPException(status_code=503,
                            detail=f"RCON nicht erreichbar: {info['reload_error']}")
    logger.info("Whitelist neu geladen: %s", instance_id)
    return {"reloaded": True}


# ---------------------------------------------------------------------------
# Mod-Update-Prüfung (Modrinth-SHA1 / CurseForge-Fingerprint)
# ---------------------------------------------------------------------------

@api.post("/instances/{instance_id}/mods/update-check")
async def instance_mod_update_check(instance_id: str):
    """Prüft alle Mods der Instanz auf neuere kompatible Versionen
    (Modrinth per SHA1, CurseForge per Fingerprint — benötigt CF_API_KEY)."""
    instances.get_instance(instance_id)
    try:
        return await updates_mod.check_updates(instance_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Mod-Update-Check fehlgeschlagen")
        raise HTTPException(status_code=502,
                            detail=f"Mod-Anbieter nicht erreichbar: {exc}")


@api.post("/instances/{instance_id}/mods/update")
async def instance_mod_update(instance_id: str, req: UpdateModsRequest):
    """Aktualisiert Mods im Hintergrund (Job); ohne Liste werden alle Mods
    mit verfügbarem Update bearbeitet."""
    instances.get_instance(instance_id)
    try:
        job = await updates_mod.start_update(instance_id, req.filenames)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Mod-Update konnte nicht gestartet werden")
        raise HTTPException(status_code=502,
                            detail=f"Mod-Anbieter nicht erreichbar: {exc}")
    logger.info("Mod-Update gestartet: %s (%d Dateien)", instance_id, job["total"])
    return {"job_id": job["id"], "total": job["total"], "phase": job["phase"]}


# ---------------------------------------------------------------------------
# Persistenter Statistik-Verlauf (SQLite)
# ---------------------------------------------------------------------------

@api.get("/history")
async def history_series(hours: int = Query(24, ge=1, le=720)):
    """Gebuckette Zeitreihen (CPU/RAM gesamt, Spieler gesamt + je Instanz)
    aus dem persistenten Verlauf. hours: 1-720 (30 Tage)."""
    try:
        data = await asyncio.to_thread(history_mod.query_series, float(hours))
    except Exception as exc:
        logger.exception("Verlauf nicht lesbar")
        raise HTTPException(status_code=503, detail=f"Verlauf nicht lesbar: {exc}")
    names = {}
    try:
        for inst in await asyncio.to_thread(instances.list_instances):
            names[inst["id"]] = inst["name"]
    except Exception:
        pass  # Namen sind optional für die Antwort
    data["names"] = names
    return data


@api.get("/players/playtime")
async def players_playtime(
        hours: str = Query("24", pattern=r"^(all|[1-9]\d{0,3})$"),
        instance: str | None = Query(None)):
    """Spielzeit-Leaderboard: Zeitfenster 24/168/720 Stunden oder 'all'
    (seit Aufzeichnung); optional gefiltert auf eine Instanz."""
    if hours != "all" and not (1 <= int(hours) <= 720):
        raise HTTPException(status_code=422,
                            detail="hours muss 1-720 oder 'all' sein")
    if instance:
        instances.get_instance(instance)  # 404 bei unbekannter Instanz
    try:
        data = await asyncio.to_thread(
            history_mod.query_playtime,
            None if hours == "all" else int(hours), instance)
    except Exception as exc:
        logger.exception("Spielzeit nicht lesbar")
        raise HTTPException(status_code=503,
                            detail=f"Spielzeit nicht lesbar: {exc}")
    return data


# ---------------------------------------------------------------------------
# Backups pro Instanz (tar.gz im Volume, unter /data/backups/{id})
# ---------------------------------------------------------------------------

def _handle_backup_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail="Backup nicht gefunden")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    logger.exception("Backup-Fehler")
    return HTTPException(status_code=500, detail=f"Backup fehlgeschlagen: {exc}")


@api.get("/instances/{instance_id}/backups")
async def list_instance_backups(instance_id: str):
    instances.get_instance(instance_id)
    try:
        return {"backups": await asyncio.to_thread(backups_mod.list_backups, instance_id)}
    except Exception as exc:
        raise _handle_backup_error(exc)


@api.post("/instances/{instance_id}/backups", status_code=201)
async def create_instance_backup(instance_id: str):
    instances.get_instance(instance_id)
    try:
        result = await asyncio.to_thread(
            backups_mod.create_backup, instance_id, instances.instance_dir(instance_id))
    except Exception as exc:
        raise _handle_backup_error(exc)
    logger.info("Backup erstellt: %s (%d Bytes) → %s",
                result["name"], result["size_bytes"], instance_id)
    return result


@api.get("/instances/{instance_id}/backups/{name}/download")
async def download_instance_backup(instance_id: str, name: str):
    instances.get_instance(instance_id)
    try:
        path = await asyncio.to_thread(backups_mod.backup_path, instance_id, name)
    except Exception as exc:
        raise _handle_backup_error(exc)
    return FileResponse(path, filename=name, media_type="application/gzip")


@api.delete("/instances/{instance_id}/backups/{name}")
async def delete_instance_backup(instance_id: str, name: str):
    instances.get_instance(instance_id)
    try:
        await asyncio.to_thread(backups_mod.delete_backup, instance_id, name)
    except Exception as exc:
        raise _handle_backup_error(exc)
    logger.info("Backup gelöscht: %s → %s", name, instance_id)
    return {"deleted": name}


@api.post("/instances/{instance_id}/backups/{name}/restore")
async def restore_instance_backup(instance_id: str, name: str):
    instance = instances.get_instance(instance_id)
    try:
        # Laufenden Container sauber stoppen (kein Fehler, falls keiner existiert)
        try:
            await asyncio.to_thread(runtime.stop_instance, instance)
        except RuntimeError:
            pass
        watchdog_mod.expect_stop(instance_id)
        instances.set_status(instance_id, "stopped", None)
        # Sicherheits-Snapshot des aktuellen Zustands, damit der Restore
        # selbst rückholbar bleibt (ohne packs/ — nur Installations-Quellen)
        await asyncio.to_thread(
            backups_mod.safety_backup, instance_id,
            instances.instance_dir(instance_id), "pre-restore")
        await asyncio.to_thread(
            backups_mod.restore_backup, instance_id, name,
            instances.instance_dir(instance_id))
    except Exception as exc:
        raise _handle_backup_error(exc)
    logger.info("Backup wiederhergestellt: %s → %s", name, instance_id)
    return {"restored": name, "status": "stopped"}


# ---------------------------------------------------------------------------
# Welt je Instanz (Info, Zip-Download, Upload)
# ---------------------------------------------------------------------------

@api.get("/instances/{instance_id}/world")
async def instance_world_info(instance_id: str):
    """Welt-Ordner der Instanz (Name, Größe) — für die Detail-Anzeige."""
    instances.get_instance(instance_id)
    return await asyncio.to_thread(worlds.world_info, instance_id)


@api.get("/instances/{instance_id}/world/download")
async def instance_world_download(instance_id: str):
    """Welt-Ordner als .zip herunterladen."""
    instances.get_instance(instance_id)
    result = await asyncio.to_thread(worlds.create_world_zip, instance_id)

    def _cleanup():
        Path(result["path"]).unlink(missing_ok=True)

    return FileResponse(result["path"], filename=result["name"],
                        media_type="application/zip",
                        background=BackgroundTask(_cleanup))


@api.post("/instances/{instance_id}/world/upload")
async def instance_world_upload(instance_id: str, file: UploadFile = File(...)):
    """Ersetzt die Welt der gestoppten Instanz durch ein Archiv
    (.zip/.tar.gz mit level.dat am Anfang oder in einem Unterordner)."""
    instances.get_instance(instance_id)
    original = worlds.validate_archive_filename(file.filename or "")
    staging_path = worlds.staging_dir() / f"world_{uuid.uuid4().hex[:8]}_{original}"
    try:
        await _stream_upload(file, staging_path, packs._MAX_PACK_BYTES)
        result = await asyncio.to_thread(
            worlds.restore_world_upload, instance_id, staging_path, original)
    finally:
        staging_path.unlink(missing_ok=True)
    logger.info("Welt ersetzt: %s → %s", original, instance_id)
    return result


# ---------------------------------------------------------------------------
# Datei-Browser je Instanz (CRUD auf dem Instanz-Ordner, Pfadschutz in
# filebrowser.py — verwaltete Dateien/packs/ sind geschützt)
# ---------------------------------------------------------------------------

@api.get("/instances/{instance_id}/files")
async def instance_files_list(instance_id: str, path: str = ""):
    instances.get_instance(instance_id)
    return await asyncio.to_thread(filebrowser_mod.list_dir, instance_id, path)


@api.get("/instances/{instance_id}/files/download")
async def instance_files_download(instance_id: str, path: str):
    instances.get_instance(instance_id)
    file_path, rel = await asyncio.to_thread(
        filebrowser_mod.download_path, instance_id, path)
    return FileResponse(file_path, filename=Path(rel).name)


@api.get("/instances/{instance_id}/files/content")
async def instance_files_content_get(instance_id: str, path: str):
    instances.get_instance(instance_id)
    return await asyncio.to_thread(filebrowser_mod.read_text, instance_id, path)


@api.put("/instances/{instance_id}/files/content")
async def instance_files_content_put(instance_id: str, req: FileContentRequest):
    instances.get_instance(instance_id)
    result = await asyncio.to_thread(
        filebrowser_mod.write_text, instance_id, req.path, req.content)
    logger.info("Datei gespeichert: %s (%d Bytes) → %s",
                result["path"], result["size"], instance_id)
    return result


@api.post("/instances/{instance_id}/files/upload")
async def instance_files_upload(instance_id: str,
                                path: str = Form(...),
                                overwrite: bool = Form(False),
                                file: UploadFile = File(...)):
    """Streamender Upload in den Instanz-Ordner (Deckel über
    FILEBROWSER_MAX_UPLOAD_MB, Default 300 MiB; 409 ohne overwrite)."""
    instances.get_instance(instance_id)
    dest = filebrowser_mod.upload_dest(instance_id, path, overwrite)
    max_bytes = settings.filebrowser_max_upload_mb * 1024 * 1024
    tmp = dest.with_name(dest.name + ".upload.tmp")
    try:
        written = await _stream_upload(
            file, tmp, max_bytes,
            label=f"{settings.filebrowser_max_upload_mb} MiB")
        os.replace(tmp, dest)
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Upload fehlgeschlagen: {exc}")
    logger.info("Datei hochgeladen: %s (%d Bytes) → %s", dest.name, written,
                instance_id)
    return {"path": dest.name, "size": written}


@api.post("/instances/{instance_id}/files/mkdir")
async def instance_files_mkdir(instance_id: str, req: FilePathRequest):
    instances.get_instance(instance_id)
    result = await asyncio.to_thread(filebrowser_mod.mkdir, instance_id, req.path)
    logger.info("Ordner angelegt: %s → %s", result["created"], instance_id)
    return result


@api.post("/instances/{instance_id}/files/rename")
async def instance_files_rename(instance_id: str, req: FileRenameRequest):
    instances.get_instance(instance_id)
    result = await asyncio.to_thread(
        filebrowser_mod.rename, instance_id, req.from_path, req.to_path)
    logger.info("Umbenannt: %s → %s (%s)", result["renamed"], result["to"],
                instance_id)
    return result


@api.delete("/instances/{instance_id}/files")
async def instance_files_delete(instance_id: str, path: str):
    instances.get_instance(instance_id)
    result = await asyncio.to_thread(filebrowser_mod.delete, instance_id, path)
    logger.info("Gelöscht: %s → %s", result["deleted"], instance_id)
    return result


# ---------------------------------------------------------------------------
# Datapacks pro Instanz (Welt-Ordner /datapacks, vanilla disabled_datapacks)
# ---------------------------------------------------------------------------

@api.get("/instances/{instance_id}/datapacks")
async def instance_datapacks_list(instance_id: str):
    instances.get_instance(instance_id)
    return await asyncio.to_thread(datapacks_mod.list_datapacks, instance_id)


@api.post("/instances/{instance_id}/datapacks/upload")
async def instance_datapacks_upload(instance_id: str, file: UploadFile = File(...)):
    """Datapack-Upload (nur bei gestoppter Instanz, .zip ≤ 50 MiB)."""
    detail = instances.get_instance(instance_id)
    if datapacks_mod.runtime_is_running(detail):
        raise HTTPException(status_code=409,
                            detail="Instanz läuft — Datapacks können nur bei "
                                   "gestoppter Instanz hochgeladen werden")
    filename = Path(file.filename or "").name or "datapack.zip"
    try:
        datapacks_mod.validate_name(filename)
    except HTTPException:
        await file.close()
        raise
    content = await _read_capped(file, datapacks_mod.UPLOAD_MAX_BYTES, "50 MiB")
    return await asyncio.to_thread(datapacks_mod.upload_datapack,
                                   instance_id, filename, content)


@api.post("/instances/{instance_id}/datapacks/enable")
async def instance_datapacks_enable(instance_id: str, req: DatapackNameRequest):
    instances.get_instance(instance_id)
    return await asyncio.to_thread(datapacks_mod.enable_datapack,
                                   instance_id, req.name)


@api.post("/instances/{instance_id}/datapacks/disable")
async def instance_datapacks_disable(instance_id: str, req: DatapackNameRequest):
    instances.get_instance(instance_id)
    return await asyncio.to_thread(datapacks_mod.disable_datapack,
                                   instance_id, req.name)


@api.delete("/instances/{instance_id}/datapacks/{name}")
async def instance_datapacks_delete(instance_id: str, name: str,
                                    enabled: bool = Query(True)):
    instances.get_instance(instance_id)
    return await asyncio.to_thread(datapacks_mod.delete_datapack,
                                   instance_id, name, enabled)


# ---------------------------------------------------------------------------
# Server-Icon (server-icon.png, 64x64-PNG, Austausch auch bei laufender Instanz)
# ---------------------------------------------------------------------------

@api.get("/instances/{instance_id}/icon")
async def instance_icon_get(instance_id: str):
    instances.get_instance(instance_id)
    if not await asyncio.to_thread(icon_mod.icon_exists, instance_id):
        raise HTTPException(status_code=404, detail="Kein Server-Icon vorhanden")
    return FileResponse(icon_mod.icon_path(instance_id), media_type="image/png")


@api.put("/instances/{instance_id}/icon")
async def instance_icon_put(instance_id: str, file: UploadFile = File(...)):
    """Server-Icon setzen: nur 64x64-PNG (Minecraft skaliert nicht)."""
    instances.get_instance(instance_id)
    content = await _read_capped(file, icon_mod.ICON_MAX_BYTES, "1 MiB")
    return await asyncio.to_thread(icon_mod.write_icon, instance_id, content)


@api.delete("/instances/{instance_id}/icon")
async def instance_icon_delete(instance_id: str):
    instances.get_instance(instance_id)
    return await asyncio.to_thread(icon_mod.delete_icon, instance_id)


# ---------------------------------------------------------------------------
# Gamerule-Quick-Editor (kuratierte Vanilla-1.21.x-Liste, nur laufend)
# ---------------------------------------------------------------------------

@api.get("/instances/{instance_id}/gamerules")
async def instance_gamerules_get(instance_id: str):
    instances.get_instance(instance_id)
    return await asyncio.to_thread(gamerules_mod.read_gamerules, instance_id)


@api.post("/instances/{instance_id}/gamerules")
async def instance_gamerules_set(instance_id: str, req: GameruleSetRequest):
    instances.get_instance(instance_id)
    return await asyncio.to_thread(gamerules_mod.set_gamerule,
                                   instance_id, req.name, req.value)


# ---------------------------------------------------------------------------
# Modpacks pro Instanz
# ---------------------------------------------------------------------------

@api.get("/modpacks/search")
async def global_pack_search(q: str = "", offset: int = 0,
                             source: str = Query("modrinth",
                                                 pattern=r"^(modrinth|curseforge)$"),
                             loader: str | None = Query(None, max_length=32),
                             game_version: str | None = Query(None, max_length=32)):
    """Globale Modpack-Suche ohne Instanzbezug (für 'Server aus Modpack
    erstellen'), optional gefiltert nach MC-Version und Loader.
    CurseForge benötigt dafür CF_API_KEY."""
    if loader:
        loader = loader.strip().lower()
        if loader not in ALLOWED_LOADERS:
            raise HTTPException(status_code=400,
                                detail=f"Loader muss einer von "
                                       f"{', '.join(ALLOWED_LOADERS)} sein")
    if game_version:
        game_version = validate_identifier(game_version.strip(),
                                           "Minecraft-Version")
    if source == "curseforge":
        return await curseforge.search_modpacks_global(
            q, offset, loader=loader, game_version=game_version)
    return await packs.search_modpacks_global(
        q, offset, loader=loader, game_version=game_version)


@api.get("/modpacks/versions")
async def global_pack_versions(
        project_id: str = Query(..., min_length=1, max_length=64,
                                pattern=r"^[A-Za-z0-9_-]{1,64}$"),
        source: str = Query("modrinth", pattern=r"^(modrinth|curseforge)$")):
    """Versionen eines Modpacks für die Versionswahl beim 'Server aus Modpack
    erstellen' (Modrinth-Versionen bzw. CurseForge-Pack-Dateien)."""
    try:
        return await packs.list_pack_versions(source, project_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Modpack-Versionen konnten nicht geladen werden")
        raise HTTPException(status_code=502,
                            detail=f"Modpack-Anbieter nicht erreichbar: {exc}")


@api.get("/instances/{instance_id}/modpacks/search")
async def instance_pack_search(instance_id: str, q: str = "", offset: int = 0,
                               source: str = Query("modrinth",
                                                   pattern=r"^(modrinth|curseforge)$")):
    instance = instances.get_instance(instance_id)
    if source == "curseforge":
        return await curseforge.search_modpacks(instance, q, offset)
    return await packs.search_modpacks(instance_id, q, offset)


@api.post("/instances/{instance_id}/modpacks/install")
async def instance_pack_install(instance_id: str, req: InstallPackRequest):
    try:
        if req.source == "curseforge":
            job = await packs.install_pack_cf(instance_id, req.project_id,
                                              file_id=req.file_id, force=req.force)
        else:
            job = await packs.install_pack(instance_id, req.project_id,
                                           version_id=req.version_id,
                                           force=req.force)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Modpack-Installation konnte nicht gestartet werden")
        raise HTTPException(status_code=502,
                            detail=f"Modpack-Anbieter nicht erreichbar: {exc}")
    logger.info("Modpack-Installation gestartet: %s (%s) → %s",
                req.project_id, req.source or "modrinth", instance_id)
    return {"job_id": job["id"], "filename": job["filename"],
            "total": job["total"], "phase": job["phase"]}


@api.get("/instances/{instance_id}/modpacks/update-check")
async def instance_pack_update_check(instance_id: str):
    """Prüft, ob für das installierte Modpack eine neuere Version verfügbar
    ist (Modrinth: neueste kompatible Version, CurseForge: neueste Datei).
    'checkable=false' bei hochgeladenen Archiven ohne Projekt-Quelle."""
    instances.get_instance(instance_id)
    try:
        return await packs.pack_update_check(instance_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Modpack-Update-Check fehlgeschlagen")
        raise HTTPException(status_code=502,
                            detail=f"Modpack-Anbieter nicht erreichbar: {exc}")


@api.post("/instances/{instance_id}/modpacks/update")
async def instance_pack_update(instance_id: str, req: PackUpdateRequest):
    """Installiert eine neuere Pack-Version erneut in die Instanz (Job wie
    bei der Installation; ohne version_id/file_id die neueste kompatible)."""
    instances.get_instance(instance_id)
    try:
        job = await packs.pack_update(instance_id, version_id=req.version_id,
                                      file_id=req.file_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Modpack-Update konnte nicht gestartet werden")
        raise HTTPException(status_code=502,
                            detail=f"Modpack-Anbieter nicht erreichbar: {exc}")
    logger.info("Modpack-Update gestartet: %s → %s", instance_id, job["filename"])
    return {"job_id": job["id"], "filename": job["filename"],
            "total": job["total"], "phase": job["phase"]}


@api.post("/instances/{instance_id}/modpacks/upload")
async def instance_pack_upload(instance_id: str, force: bool = Form(False),
                               auto_version: bool = Form(True),
                               file: UploadFile = File(...)):
    """Installiert ein hochgeladenes Modpack-Archiv (.mrpack oder CurseForge-.zip).

    Das Archiv wird streamend in packs/ der Instanz geschrieben (Größenlimit
    4 GiB), auf Kompatibilität geprüft und anschließend wie ein Modrins-Pack
    installiert. Als Antwort kommt ein Job (wie bei der Modrinth-Installation).
    auto_version=true (Standard): Benötigte MC-Version/Loader werden aus dem
    Archiv erkannt und die Instanz bei Abweichung automatisch umgestellt
    (nur bei gestoppter Instanz, sonst Fehler im Job).
    """
    instances.get_instance(instance_id)
    filename = instances.validate_pack_filename(file.filename or "")
    pack_dir = instances.pack_dir(instance_id)
    try:
        pack_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"packs-Ordner nicht schreibbar: {exc}")
    dest = pack_dir / filename
    try:
        # Streamend schreiben, damit große Archive den Speicher nicht füllen
        max_bytes = 4 * 1024 * 1024 * 1024  # harte Grenze, wie packs._MAX_PACK_BYTES
        written = 0
        with open(dest, "wb") as fh:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=413,
                                        detail="Modpack zu groß (max 4 GiB)")
                fh.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Upload fehlgeschlagen: {exc}")
    finally:
        await file.close()

    try:
        job = await packs.install_upload(instance_id, dest, filename,
                                         force=force, auto_version=auto_version)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    logger.info("Modpack-Upload gestartet: %s (%d Bytes) → %s",
                filename, dest.stat().st_size, instance_id)
    return {"job_id": job["id"], "filename": job["filename"],
            "total": job["total"], "phase": job["phase"],
            "target": {"type": "instance", "instance_id": instance_id}}


app.include_router(api)


@app.middleware("http")
async def no_cache_frontend(request: Request, call_next):
    """Frontend-Dateien (HTML/JS/CSS) immer revalidieren — ohne Cache-Control
    cachen Browser app.js heuristisch, und nach einem Update läuft dann
    weiter das alte JavaScript (Symptom: „Log-Fenster aktualisiert sich
    nicht“). API-Antworten bleiben unangetastet."""
    response = await call_next(request)
    if not request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    first = errors[0] if errors else {}
    loc = ".".join(str(part) for part in first.get("loc", []) if part != "body")
    msg = first.get("msg", "Ungültige Eingabe")
    return JSONResponse(status_code=422,
                        content={"detail": f"Ungültige Eingabe ({loc}): {msg}"})


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    logger.exception("Unbehandelter Fehler")
    return JSONResponse(status_code=500, content={"detail": "Interner Serverfehler"})


# Statisches Frontend zuletzt mounten (fängt alle übrigen Pfade)
_static_dir = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port)
