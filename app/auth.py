"""Login & Rollen: Benutzerdatei (users.json), Passwort-Hashing (scrypt),
HMAC-signierte Session-Cookies und In-Memory-Lockout.

Funktioniert komplett ohne neue Laufzeit-Abhängigkeiten (stdlib):

- users.json liegt neben INSTANCES_DIR (Geschwister von scheduler_state.json,
  also /data/users.json): [{username, salt, scrypt_hash, role, created}].
  Leere/fehlende/defekte Datei = kein Login aktiv (Bestandsverhalten: die API
  ist offen bzw. nur per DASHBOARD_API_KEY geschützt) und der Setup-Dialog
  darf den ersten Admin anlegen. Atomar geschrieben (tmp + replace).
- Passwort: hashlib.scrypt (n=2^14, r=8, p=1, 32-Byte-Salt, 32-Byte-Hash),
  mindestens 8 Zeichen. Username: ^[A-Za-z0-9_.-]{1,32}$.
- Session: base64url(JSON {u, r, exp, v}) + "." + HMAC-SHA256-Signatur mit dem
  Secret aus {INSTANCES_DIR.parent}/.auth_secret (64 hex, 0600, einmalig
  generiert — gleiche Idee wie .rcon_salt). v = session_version des
  Benutzers: Passwort-/Rollen-Änderung erhöhen die Version und machen
  ausstehende Token sofort ungültig (kein 30-Tage-Nachleuchten); gelöschte
  Benutzer verlieren ihre Sessions damit ebenfalls. Cookie 'mcd_session'
  (HttpOnly, SameSite=Strict, Pfad /, 30 Tage). Ohne CSRF-Token:
  SameSite=Strict plus same-origin Frontend blockt Cross-Site-POSTs.
- Lockout in-memory: 10 Fehlversuche je Username → 5 Minuten Sperre; ein
  Dashboard-Neustart setzt die Sperrliste zurück (Homelab: bewusst
  akzeptiert, nicht persistiert). Login-Fehler mit konstantem Delay, damit
  Timing keine Nutzernamen verrät.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import HTTPException, Request

logger = logging.getLogger("dashboard.auth")

# Username: einfache Zeichen, 1-32 Länge (technischer Name, kein Anzeigename)
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_MIN_PASSWORD = 8
_MAX_PASSWORD = 128
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 32

SESSION_COOKIE = "mcd_session"
SESSION_TTL = 30 * 86400
ROLES = ("admin", "viewer")
_MAX_USERS = 50

# Lockout-Schwellen (in-memory, Neustart resettet)
LOCKOUT_THRESHOLD = 10
LOCKOUT_SECONDS = 300
LOGIN_DELAY_SECONDS = 0.5

_users_cache: dict[str, object] = {"ts": 0.0, "users": None}
_login_fails: dict[str, list[float]] = {}
_secret_cache: dict[str, object] = {"value": None}


def users_path() -> Path:
    """Pfad der Benutzerdatei (Geschwister von INSTANCES_DIR, im Volume)."""
    return Path(settings_instances_dir()).parent / "users.json"


def settings_instances_dir() -> Path:
    """Lazy Import (vermeidet Import-Zirkel mit config)."""
    from .config import settings
    return Path(settings.instances_dir)


def list_users() -> list:
    """Benutzer laden (mit 2-s-Cache); fehlt/defekt → [] (kein Login aktiv)."""
    path = users_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _users_cache["ts"] = 0.0
        _users_cache["users"] = None
        return []
    if _users_cache["users"] is not None and _users_cache["ts"] == mtime:
        return _users_cache["users"]  # type: ignore[return-value]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("users.json defekt/unlesbar — Login gilt als deaktiviert")
        _users_cache["ts"] = mtime
        _users_cache["users"] = []
        return []
    users = data if isinstance(data, list) else []
    _users_cache["ts"] = mtime
    _users_cache["users"] = users
    return users


def save_users(users: list) -> None:
    """Benutzer atomar schreiben (tmp + replace) und Cache verwerfen."""
    path = users_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(users, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=500,
                            detail=f"users.json nicht schreibbar: {exc}") from exc
    _users_cache["ts"] = 0.0
    _users_cache["users"] = None


def invalidate_users_cache() -> None:
    _users_cache["ts"] = 0.0
    _users_cache["users"] = None


def login_active() -> bool:
    """Login aktiv, sobald mindestens ein Benutzer existiert."""
    return len(list_users()) > 0


def setup_available() -> bool:
    """Setup-Dialog erlaubt, solange kein Benutzer existiert."""
    return not login_active()


# ---------------------------------------------------------------------------
# Passwort-Hashing (scrypt, stdlib)
# ---------------------------------------------------------------------------

def validate_username(username: str) -> str:
    """Username normalisieren/validieren; HTTPException bei Verstößen."""
    username = (username or "").strip()
    if not username or not _USERNAME_RE.match(username) \
            or username in (".", ".."):
        raise HTTPException(
            status_code=400,
            detail="Ungültiger Benutzername: 1-32 Zeichen, erlaubt sind "
                   "Buchstaben, Zahlen, Punkt, Unterstrich, Bindestrich")
    return username


def validate_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < _MIN_PASSWORD \
            or len(password) > _MAX_PASSWORD:
        raise HTTPException(
            status_code=400,
            detail=f"Passwort muss {_MIN_PASSWORD}-{_MAX_PASSWORD} Zeichen lang sein")
    return password


def validate_role(role: str) -> str:
    if role not in ROLES:
        raise HTTPException(status_code=400,
                            detail=f"Rolle muss einer von {', '.join(ROLES)} sein")
    return role


def hash_password(password: str) -> tuple:
    """Erzeugt (salt_hex, hash_hex) für ein neues Passwort."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            dklen=_SCRYPT_DKLEN)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    """Konstantzeit-Vergleich eines Passworts gegen den gespeicherten Hash."""
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def user_session_version(user: dict | None) -> int:
    """session_version eines Benutzer-Eintrags (fehlend/defekt = 0)."""
    try:
        return int((user or {}).get("session_version") or 0)
    except (TypeError, ValueError):
        return 0


def _bump_session_version(user: dict) -> None:
    """session_version erhöhen — alle ausgestellten Token werden ungültig."""
    user["session_version"] = user_session_version(user) + 1


def find_user(username: str) -> dict | None:
    username = (username or "").strip().lower()
    for user in list_users():
        if str(user.get("username") or "").lower() == username:
            return user
    return None


def create_user(username: str, password: str, role: str) -> dict:
    """Benutzer anlegen (HTTPException bei Duplikat/validierung)."""
    username = validate_username(username)
    validate_password(password)
    validate_role(role)
    users = list_users()
    if find_user(username):
        raise HTTPException(status_code=409,
                            detail=f"Benutzer '{username}' existiert bereits")
    if len(users) >= _MAX_USERS:
        raise HTTPException(status_code=400,
                            detail=f"Zu viele Benutzer (max. {_MAX_USERS})")
    salt_hex, hash_hex = hash_password(password)
    user = {"username": username, "salt": salt_hex, "scrypt_hash": hash_hex,
            "role": role, "created": int(time.time())}
    save_users([*users, user])
    return {k: user[k] for k in ("username", "role", "created")}


def update_user(username: str, role: str | None = None,
                password: str | None = None) -> dict:
    """Rolle und/oder Passwort eines Benutzers ändern (404 unbekannt)."""
    username = validate_username(username)
    users = list_users()
    user = None
    for entry in users:
        if str(entry.get("username") or "").lower() == username.lower():
            user = entry
            break
    if user is None:
        raise HTTPException(status_code=404, detail="Benutzer nicht gefunden")
    changed = []
    if role is not None:
        validate_role(role)
        admins = _admin_names(users)
        if user["role"] == "admin" and role != "admin" \
                and username.lower() in admins and len(admins) <= 1:
            raise HTTPException(status_code=409,
                                detail="Letzter Administrator kann nicht "
                                       "herabgestuft werden")
        user["role"] = role
        changed.append("role")
    if password is not None:
        validate_password(password)
        salt_hex, hash_hex = hash_password(password)
        user["salt"] = salt_hex
        user["scrypt_hash"] = hash_hex
        changed.append("password")
    if not changed:
        raise HTTPException(status_code=400,
                            detail="Keine Änderungen übergeben (role/password)")
    # Rollen-/Passwort-Änderung: ausstehende Sessions sofort invalidieren
    _bump_session_version(user)
    save_users(users)
    return {"username": user["username"], "role": user["role"],
            "created": user.get("created"), "changed": changed}


def delete_user(username: str, requester: str) -> dict:
    """Benutzer löschen: nicht sich selbst, nicht den letzten Admin."""
    username = validate_username(username)
    users = list_users()
    remaining = [u for u in users
                 if str(u.get("username") or "").lower() != username.lower()]
    if len(remaining) == len(users):
        raise HTTPException(status_code=404, detail="Benutzer nicht gefunden")
    if username.lower() == (requester or "").strip().lower():
        raise HTTPException(status_code=400,
                            detail="Eigener Account kann nicht gelöscht werden")
    admins_before = _admin_names(users)
    if username.lower() in admins_before and len(admins_before) <= 1:
        raise HTTPException(status_code=409,
                            detail="Letzter Administrator kann nicht gelöscht werden")
    save_users(remaining)
    return {"deleted": username}


def _admin_names(users: list) -> set:
    return {str(u.get("username") or "").lower()
            for u in users if u.get("role") == "admin"}


def change_own_password(username: str, current: str, new_password: str) -> dict:
    """Eigenes Passwort ändern (aktuelles Passwort muss stimmen)."""
    user = find_user(username)
    if user is None:
        raise HTTPException(status_code=404, detail="Benutzer nicht gefunden")
    if not verify_password(current or "", user.get("salt") or "",
                           user.get("scrypt_hash") or ""):
        raise HTTPException(status_code=400, detail="Aktuelles Passwort ist falsch")
    validate_password(new_password)
    salt_hex, hash_hex = hash_password(new_password)
    users = list_users()
    for entry in users:
        if str(entry.get("username") or "").lower() == username.lower():
            entry["salt"] = salt_hex
            entry["scrypt_hash"] = hash_hex
            # Eigene andere Sessions invalidieren; die Route reicht dafür
            # einen frischen Token zurück (erfunden nach dem Bump).
            _bump_session_version(entry)
            break
    save_users(users)
    return {"username": username, "changed": ["password"]}


# ---------------------------------------------------------------------------
# Secret-Datei (.auth_secret, 0600) + Token (HMAC-SHA256)
# ---------------------------------------------------------------------------

def _secret_path() -> Path:
    return settings_instances_dir().parent / ".auth_secret"


def ensure_secret() -> bytes:
    """Secret laden oder einmalig generieren (fail-closed: Schreibfehler
    machen Login unmöglich, statt still unsichere Tokens zu signieren)."""
    cached = _secret_cache.get("value")
    if isinstance(cached, bytes):
        return cached
    path = _secret_path()
    raw = ""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        raw = ""
    if raw:
        try:
            secret = bytes.fromhex(raw)
            if len(secret) >= 32:
                _secret_cache["value"] = secret
                return secret
        except ValueError:
            pass  # defektes Secret → neu erzeugen (Session werden ungültig)
    secret = secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret.hex() + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # Windows: Rechte-Feinschliff ist best effort
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f"Auth-Secret nicht schreibbar: {exc}") from exc
    _secret_cache["value"] = secret
    return secret


def reset_secret_cache() -> None:
    _secret_cache["value"] = None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _token_version(data: dict) -> int:
    try:
        return int(data.get("v") or 0)
    except (TypeError, ValueError):
        return -1  # defekte Version → nie gültig


def create_token(username: str, role: str, ttl: int = SESSION_TTL,
                 now: float | None = None, version: int | None = None) -> str:
    """base64url(json{u, r, exp, v}).hmac — Session mit Benutzer-Version.
    Die Version wird aus users.json gelesen (Benutzer unbekannt = 0), damit
    nach Rollen-/Passwort-Änderung ausgestellte Token nicht mehr gelten."""
    username = validate_username(username)
    validate_role(role)
    secret = ensure_secret()
    if version is None:
        user = find_user(username)
        version = user_session_version(user) if user else 0
    payload = json.dumps(
        {"u": username, "r": role, "exp": int((now or time.time()) + ttl),
         "v": int(version)},
        separators=(",", ":")).encode("utf-8")
    body = _b64url(payload)
    signature = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def verify_token(token: str, now: float | None = None) -> dict | None:
    """Token prüfen (Signatur + Ablauf + Benutzer-Version/-Existenz).
    Rückgabe {u, r, exp, v} oder None. Ohne Benutzerdatei (kein Login
    aktiv) entfällt der Benutzer-Abgleich (Bestandsverhalten)."""
    if not token or token.count(".") != 1:
        return None
    body, signature = token.split(".", 1)
    try:
        payload = _b64url_decode(body)
    except (ValueError, TypeError):
        return None
    try:
        secret = ensure_secret()
    except HTTPException:
        return None  # Secret unlesbar → fail-closed: keine Session gültig
    expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        data = json.loads(payload.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("r") not in ROLES or not isinstance(data.get("u"), str):
        return None
    try:
        if int(data.get("exp") or 0) < int(now or time.time()):
            return None
    except (TypeError, ValueError):
        return None
    if login_active():
        user = find_user(data["u"])
        # Benutzer gelöscht, Version geändert (Passwort/Rolle) oder Rolle im
        # Token nicht mehr aktuell → Session sofort ungültig
        if user is None or user_session_version(user) != _token_version(data) \
                or user.get("role") != data.get("r"):
            return None
    return data


def current_session(request: Request) -> dict | None:
    """Session-Cookie des Requests prüfen (None = nicht eingeloggt)."""
    return verify_token(request.cookies.get(SESSION_COOKIE) or "")


def require_admin(request: Request) -> dict:
    """Admin-Pflicht für die /api/auth/users-Verwaltung: API-Key (Skripte)
    oder Admin-Session. Ist nichts konfiguriert (kein Key, keine Benutzer),
    gilt der anonyme Zugriff als Admin (Bestandsverhalten). Eingeloggte
    Viewer bekommen 403 (Rollen-Matrix), Unbekannte 401."""
    from .config import settings  # lazy, vermeidet Import-Zirkel

    if settings.api_key and request.headers.get("X-API-Key") == settings.api_key:
        return {"u": "(api-key)", "r": "admin"}
    session = current_session(request)
    if session and session.get("r") == "admin":
        return session
    if not settings.api_key and not login_active():
        return {"u": "(anonym)", "r": "admin"}
    if session:
        raise HTTPException(status_code=403,
                            detail="Diese Aktion erfordert die Admin-Rolle")
    raise HTTPException(status_code=401,
                        detail="Admin-Login erforderlich")


def require_session(request: Request) -> dict:
    """Eingeloggte Session Pflicht (z. B. change-password); API-Key zählt
    nicht — ohne Benutzername gibt es kein 'eigenes' Passwort."""
    session = current_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")
    return session


# ---------------------------------------------------------------------------
# Lockout + konstanter Delay
# ---------------------------------------------------------------------------

def _prune_fails(username: str, now: float) -> list:
    fails = [t for t in _login_fails.get(username, [])
             if now - t < LOCKOUT_SECONDS]
    _login_fails[username] = fails
    return fails


def login_locked(username: str, now: float | None = None) -> bool:
    """True, wenn für den Username aktuell eine Sperre aktiv ist."""
    now = now or time.time()
    fails = _prune_fails(username.lower(), now)
    return len(fails) >= LOCKOUT_THRESHOLD


def register_login_fail(username: str, now: float | None = None) -> int:
    """Fehlversuch merken; Rückgabe: verbleibende Sekunden der Sperre (0)."""
    now = now or time.time()
    key = (username or "").strip().lower()
    fails = _prune_fails(key, now)
    fails.append(now)
    _login_fails[key] = fails
    if len(fails) >= LOCKOUT_THRESHOLD:
        return LOCKOUT_SECONDS
    return 0


def clear_login_fails(username: str) -> None:
    _login_fails.pop((username or "").strip().lower(), None)


def reset_for_tests() -> None:
    """In-Memory-Zustände zurücksetzen (nur Tests)."""
    _login_fails.clear()
    _secret_cache["value"] = None
    invalidate_users_cache()


async def login_delay() -> None:
    """Konstanter Delay bei Login-Fehlern (Event-Loop-schonend, async)."""
    await asyncio.sleep(LOGIN_DELAY_SECONDS)


# ---------------------------------------------------------------------------
# Kombinierter Guard-Anteil (wird von security.auth_guard benutzt)
# ---------------------------------------------------------------------------

def role_from_request(request: Request, api_key_header: str | None) -> str | None:
    """Rolle aus API-Key oder Session-Cookie ermitteln (None = nicht
    authentifiziert). API-Key gilt implizit als Admin."""
    from .config import settings  # lazy, vermeidet Import-Zirkel

    if settings.api_key and api_key_header == settings.api_key:
        return "admin"
    session = current_session(request)
    if session:
        return str(session.get("r"))
    return None


def anonymous_allowed() -> bool:
    """Bestandsverhalten: ganz ohne Konfiguration (kein API-Key, keine
    Benutzer) bleibt die API offen."""
    from .config import settings  # lazy, vermeidet Import-Zirkel

    return not settings.api_key and not login_active()


__all__ = [
    "ROLES",
    "SESSION_COOKIE",
    "SESSION_TTL",
    "anonymous_allowed",
    "change_own_password",
    "clear_login_fails",
    "create_token",
    "create_user",
    "current_session",
    "delete_user",
    "ensure_secret",
    "find_user",
    "hash_password",
    "invalidate_users_cache",
    "list_users",
    "login_active",
    "login_delay",
    "login_locked",
    "register_login_fail",
    "require_admin",
    "require_session",
    "reset_for_tests",
    "reset_secret_cache",
    "role_from_request",
    "setup_available",
    "update_user",
    "user_session_version",
    "users_path",
    "validate_password",
    "validate_role",
    "validate_username",
    "verify_password",
    "verify_token",
]
