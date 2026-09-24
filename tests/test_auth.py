"""Tests für Login & Rollen (app/auth.py, Guard, /api/auth/*-Routen)."""
import shutil
import time

import pytest
from fastapi import HTTPException

from app import auth as auth_mod
from app.config import settings


@pytest.fixture(autouse=True)
def _auth_umgebung():
    """Leere Instanz-Ordner je Test (conftest-tmp ist über die gesamte Suite
    geteilt); users.json/.auth_secret räumt der globale _isoliere_auth auf."""
    if settings.instances_dir.exists():
        for entry in settings.instances_dir.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
    yield
    if settings.instances_dir.exists():
        for entry in settings.instances_dir.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)


# ---------------------------------------------------------------------------
# Modul: users.json, Passwort-Hash, Token
# ---------------------------------------------------------------------------

class TestUsersStore:
    def test_create_und_find(self):
        user = auth_mod.create_user("Alfred.E-1", "passwort123", "viewer")
        assert user["username"] == "Alfred.E-1"
        assert user["role"] == "viewer"
        found = auth_mod.find_user("alfred.e-1")  # case-insensitive
        assert found is not None
        assert auth_mod.verify_password("passwort123",
                                        found["salt"], found["scrypt_hash"])

    def test_passwort_hash_ist_gesalzen(self):
        salt1, hash1 = auth_mod.hash_password("passwort123")
        salt2, hash2 = auth_mod.hash_password("passwort123")
        assert salt1 != salt2 and hash1 != hash2
        assert auth_mod.verify_password("passwort123", salt1, hash1)
        assert not auth_mod.verify_password("falsch", salt1, hash1)
        assert not auth_mod.verify_password("passwort123", salt1, "00" * 32)

    def test_username_validierung(self):
        for bad in ("", "a" * 33, "mit leer", "pfad/x", "pfad\\x", ".."):
            with pytest.raises(HTTPException) as e:
                auth_mod.validate_username(bad)
            assert e.value.status_code == 400
        assert auth_mod.validate_username("A9-._") == "A9-._"

    def test_passwort_mindestlaenge(self):
        with pytest.raises(HTTPException):
            auth_mod.validate_password("kurz")
        assert auth_mod.validate_password("langgenug") == "langgenug"

    def test_defekte_users_json_zaehlt_als_leer(self):
        auth_mod.create_user("boss", "passwort123", "admin")
        auth_mod.users_path().write_text("{kaputt", encoding="utf-8")
        auth_mod.invalidate_users_cache()
        assert auth_mod.list_users() == []
        assert auth_mod.setup_available() is True

    def test_duplikat_409(self):
        auth_mod.create_user("boss", "passwort123", "admin")
        with pytest.raises(HTTPException) as e:
            auth_mod.create_user("BOSS", "passwort123", "viewer")
        assert e.value.status_code == 409

    def test_delete_selbst_und_letzter_admin_geschuetzt(self):
        auth_mod.create_user("boss", "passwort123", "admin")
        with pytest.raises(HTTPException) as e:
            auth_mod.delete_user("boss", "boss")
        assert e.value.status_code == 400
        with pytest.raises(HTTPException) as e:
            auth_mod.delete_user("boss", "anderer")
        assert e.value.status_code == 409

    def test_update_letzter_admin_geschuetzt(self):
        auth_mod.create_user("boss", "passwort123", "admin")
        with pytest.raises(HTTPException) as e:
            auth_mod.update_user("boss", role="viewer")
        assert e.value.status_code == 409
        auth_mod.create_user("zwei", "passwort123", "admin")
        assert auth_mod.update_user("boss", role="viewer")["changed"] == ["role"]


class TestToken:
    def test_roundtrip(self):
        token = auth_mod.create_token("boss", "admin")
        payload = auth_mod.verify_token(token)
        assert payload is not None
        assert payload["u"] == "boss" and payload["r"] == "admin"
        assert payload["exp"] > int(time.time())
        assert auth_mod.verify_token(token + "x") is None
        assert auth_mod.verify_token("garbage") is None
        assert auth_mod.verify_token("") is None

    def test_ablauf(self):
        token = auth_mod.create_token("boss", "admin", ttl=10, now=1000.0)
        assert auth_mod.verify_token(token, now=1005.0) is not None
        assert auth_mod.verify_token(token, now=1005.0 + 11) is None

    def test_secret_fail_closed(self, monkeypatch):
        token = auth_mod.create_token("boss", "admin")
        # Secret-Schreibpfad blockieren: übergeordnete Komponente ist eine Datei
        blocker = auth_mod.users_path().parent / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setattr(auth_mod, "_secret_path",
                            lambda: blocker / "sub" / ".auth_secret")
        auth_mod.reset_secret_cache()
        assert auth_mod.verify_token(token) is None


class TestSessionInvalidierung:
    """session_version im Token: Passwort-/Rollen-Änderung und Löschung
    machen ausgestellte Sessions sofort ungültig (kein 30-Tage-Nachleuchten)."""

    def test_token_enthaelt_version_und_roundtrip(self):
        auth_mod.create_user("boss", "passwort123", "admin")
        token = auth_mod.create_token("boss", "admin")
        payload = auth_mod.verify_token(token)
        assert payload is not None and payload["v"] == 0
        # Bestands-Token ohne v + Benutzer ohne session_version bleibt gültig
        alt = auth_mod.create_token("boss", "admin", version=0)
        assert auth_mod.verify_token(alt) is not None

    def test_geloeschter_benutzer_token_ungueltig(self):
        auth_mod.create_user("boss", "passwort123", "admin")
        auth_mod.create_user("zwei", "passwort123", "admin")
        token = auth_mod.create_token("boss", "admin")
        assert auth_mod.verify_token(token) is not None
        auth_mod.delete_user("boss", "zwei")
        assert auth_mod.verify_token(token) is None

    def test_rolle_aendern_invalidiert_token(self):
        auth_mod.create_user("boss", "passwort123", "admin")
        auth_mod.create_user("waechter", "passwort123", "viewer")
        token = auth_mod.create_token("waechter", "viewer")
        assert auth_mod.verify_token(token) is not None
        auth_mod.update_user("waechter", role="admin")
        # Alte Rolle im Token ≠ gespeicherte Rolle → ungültig (Downgrade-Schutz)
        assert auth_mod.verify_token(token) is None
        neuer = auth_mod.create_token("waechter", "admin")
        assert auth_mod.verify_token(neuer) is not None

    def test_passwort_reset_invalidiert_token(self):
        auth_mod.create_user("boss", "passwort123", "admin")
        token = auth_mod.create_token("boss", "admin")
        assert auth_mod.verify_token(token) is not None
        auth_mod.update_user("boss", password="neues-pass-1")
        assert auth_mod.verify_token(token) is None
        assert auth_mod.verify_token(auth_mod.create_token(
            "boss", "admin")) is not None

    def test_change_own_password_invalidiert_token(self):
        auth_mod.create_user("boss", "passwort123", "admin")
        token = auth_mod.create_token("boss", "admin")
        auth_mod.change_own_password("boss", "passwort123", "neues-passwort-1")
        assert auth_mod.verify_token(token) is None

    def test_versionen_zaehlen_hoch(self):
        auth_mod.create_user("boss", "passwort123", "admin")
        user = auth_mod.find_user("boss")
        assert user is not None
        auth_mod.update_user("boss", role="admin")
        user = auth_mod.find_user("boss")
        assert user is not None
        assert auth_mod.user_session_version(user) == 1
        auth_mod.update_user("boss", password="neues-pass-1")
        user = auth_mod.find_user("boss")
        assert user is not None
        assert auth_mod.user_session_version(user) == 2

    def test_defekte_version_im_token_nie_gueltig(self):
        auth_mod.create_user("boss", "passwort123", "admin")
        # Token von Hand mit kaputtem v bauen (Signatur korrekt)
        import hashlib as hl
        import hmac as hm
        import json as js
        secret = auth_mod.ensure_secret()
        payload = js.dumps({"u": "boss", "r": "admin",
                            "exp": int(time.time()) + 600, "v": "kaputt"},
                           separators=(",", ":")).encode("utf-8")
        body = auth_mod._b64url(payload)
        sig = hm.new(secret, payload, hl.sha256).hexdigest()
        assert auth_mod.verify_token(f"{body}.{sig}") is None

    # --- Route-Ebene ---

    def test_routen_rolle_downgrade_invalidiert_session(self, client):
        client.post("/api/auth/setup", json={"username": "boss",
                                             "password": "passwort123"})
        client.post("/api/auth/users", json={"username": "chef",
                                             "password": "passwort123",
                                             "role": "admin"})
        # Chef-Session aufnehmen (Cookie des Setups überschreiben)
        client.cookies.clear()
        client.post("/api/auth/login", json={"username": "chef",
                                             "password": "passwort123"})
        chef_cookie = client.cookies.get(auth_mod.SESSION_COOKIE)
        assert chef_cookie
        # Chef in eigener Session: users-Verwaltung lesbar (Admin)
        assert client.get("/api/auth/users").status_code == 200
        # Boss stuft Chef herab
        client.cookies.clear()
        client.post("/api/auth/login", json={"username": "boss",
                                             "password": "passwort123"})
        r = client.patch("/api/auth/users/chef", json={"role": "viewer"})
        assert r.status_code == 200
        # Chef-Session (alter Cookie) ist serverseitig tot → 401
        client.cookies.clear()
        client.cookies.set(auth_mod.SESSION_COOKIE, chef_cookie)
        assert client.get("/api/auth/users").status_code == 401

    def test_routen_passwort_reset_alte_session_401(self, client):
        client.post("/api/auth/setup", json={"username": "boss",
                                             "password": "passwort123"})
        client.post("/api/auth/users", json={"username": "waechter",
                                             "password": "passwort123",
                                             "role": "viewer"})
        client.cookies.clear()
        client.post("/api/auth/login", json={"username": "waechter",
                                             "password": "passwort123"})
        viewer_cookie = client.cookies.get(auth_mod.SESSION_COOKIE)
        assert viewer_cookie
        assert client.get("/api/settings").status_code == 200
        # Admin-Reset des Viewer-Passworts
        client.cookies.clear()
        client.post("/api/auth/login", json={"username": "boss",
                                             "password": "passwort123"})
        assert client.patch("/api/auth/users/waechter",
                            json={"password": "neues-pass-1"}).status_code == 200
        # Viewer-Session (alter Cookie) ist serverseitig tot → 401
        client.cookies.clear()
        client.cookies.set(auth_mod.SESSION_COOKIE, viewer_cookie)
        assert client.get("/api/settings").status_code == 401
        # Neue Anmeldung mit dem neuen Passwort funktioniert
        client.cookies.clear()
        assert client.post("/api/auth/login", json={"username": "waechter",
                                                    "password": "neues-pass-1"}
                           ).status_code == 200

    def test_change_password_liefert_frischen_cookie(self, client):
        client.post("/api/auth/setup", json={"username": "boss",
                                             "password": "passwort123"})
        r = client.post("/api/auth/change-password", json={
            "current": "passwort123", "password": "neues-passwort-1"})
        assert r.status_code == 200
        assert "mcd_session=" in r.headers.get("set-cookie", "").lower()
        # Neue Session bleibt direkt danach gültig (nicht ausgeloggt)
        me = client.get("/api/auth/me").json()
        assert me["authenticated"] is True and me["username"] == "boss"


# ---------------------------------------------------------------------------
# API-Routen: Setup/Login/Logout/me/Benutzerverwaltung
# ---------------------------------------------------------------------------

class TestSetup:
    def test_setup_und_autologin(self, client):
        resp = client.post("/api/auth/setup",
                           json={"username": "boss", "password": "passwort123"})
        assert resp.status_code == 201
        cookie = resp.headers["set-cookie"].lower()
        assert "mcd_session=" in cookie
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        assert "path=/" in cookie
        me = client.get("/api/auth/me").json()
        assert me["authenticated"] is True and me["role"] == "admin"

    def test_setup_nur_einmal(self, client):
        assert client.post("/api/auth/setup",
                           json={"username": "a", "password": "passwort123"}
                           ).status_code == 201
        assert client.post("/api/auth/setup",
                           json={"username": "b", "password": "passwort123"}
                           ).status_code == 409
        assert client.get("/api/auth/setup-available").json() == {"available": False}

    def test_setup_verlangt_api_key_wenn_gesetzt(self, client, monkeypatch):
        monkeypatch.setattr(settings, "api_key", "key123")
        r = client.post("/api/auth/setup",
                        json={"username": "boss", "password": "passwort123"})
        assert r.status_code == 401
        r = client.post("/api/auth/setup", headers={"X-API-Key": "key123"},
                        json={"username": "boss", "password": "passwort123"})
        assert r.status_code == 201

    def test_setup_validierung(self, client):
        assert client.post("/api/auth/setup",
                           json={"username": "bo ss", "password": "passwort123"}
                           ).status_code == 422
        assert client.post("/api/auth/setup",
                           json={"username": "boss", "password": "kurz"}
                           ).status_code == 422


class TestLogin:
    def test_login_logout_me(self, client):
        client.post("/api/auth/setup",
                    json={"username": "boss", "password": "passwort123"})
        client.cookies.clear()
        resp = client.post("/api/auth/login",
                           json={"username": "boss", "password": "passwort123"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"
        assert client.get("/api/auth/me").json()["authenticated"] is True
        resp = client.post("/api/auth/logout")
        assert resp.status_code == 200
        assert client.get("/api/auth/me").json()["authenticated"] is False

    def test_falsches_passwort_delay_und_401(self, client, monkeypatch):
        client.post("/api/auth/setup",
                    json={"username": "boss", "password": "passwort123"})
        client.cookies.clear()
        slept = []

        async def fake_delay():
            slept.append(1)

        monkeypatch.setattr(auth_mod, "login_delay", fake_delay)
        r = client.post("/api/auth/login",
                        json={"username": "boss", "password": "falsches-pw"})
        assert r.status_code == 401
        assert slept  # konstanter Delay wurde ausgeführt

    def test_unbekannter_user_gleicher_fehler(self, client, monkeypatch):
        slept = []

        async def fake_delay():
            slept.append(1)

        monkeypatch.setattr(auth_mod, "login_delay", fake_delay)
        r = client.post("/api/auth/login",
                        json={"username": "niemand", "password": "egal-egal1"})
        assert r.status_code == 401
        assert slept

    def test_lockout_nach_10_fehlversuchen(self, client, monkeypatch):
        client.post("/api/auth/setup",
                    json={"username": "boss", "password": "passwort123"})
        client.cookies.clear()

        async def fake_delay():
            pass

        monkeypatch.setattr(auth_mod, "login_delay", fake_delay)
        # 9 Fehlversuche → 401, beim 10. greift die Sperre
        for _ in range(9):
            r = client.post("/api/auth/login",
                            json={"username": "boss", "password": "falsches-pw"})
            assert r.status_code == 401
        r = client.post("/api/auth/login",
                        json={"username": "boss", "password": "falsches-pw"})
        assert r.status_code == 429
        # Richtige Anmeldung bleibt gesperrt; Reset (z. B. Neustart) hilft
        r = client.post("/api/auth/login",
                        json={"username": "boss", "password": "passwort123"})
        assert r.status_code == 429
        auth_mod.clear_login_fails("boss")
        r = client.post("/api/auth/login",
                        json={"username": "boss", "password": "passwort123"})
        assert r.status_code == 200


class TestGuardMatrix:
    def test_ohne_konfiguration_alles_offen(self, client):
        assert client.get("/api/settings").status_code == 200
        assert client.post("/api/auth/users", json={
            "username": "x", "password": "passwort123"}).status_code == 201

    def test_api_key_ist_admin(self, client, monkeypatch):
        monkeypatch.setattr(settings, "api_key", "key123")
        assert client.get("/api/settings").status_code == 401
        assert client.get("/api/settings",
                          headers={"X-API-Key": "key123"}).status_code == 200
        # Schreiben per API-Key erlaubt (implizit Admin)
        inst = client.post("/api/instances", headers={"X-API-Key": "key123"},
                           json={"name": "T", "loader": "fabric",
                                 "game_version": "1.21.4",
                                 "accept_eula": True})
        assert inst.status_code == 201

    def test_api_key_falsch_und_login_aktiv(self, client, monkeypatch):
        client.post("/api/auth/setup", json={"username": "boss",
                                             "password": "passwort123"})
        client.cookies.clear()
        monkeypatch.setattr(settings, "api_key", "key123")
        assert client.get("/api/settings").status_code == 401
        assert client.get("/api/settings",
                          headers={"X-API-Key": "falsch"}).status_code == 401

    def test_viewer_get_ok_post_403(self, client):
        client.post("/api/auth/setup", json={"username": "boss",
                                             "password": "passwort123"})
        client.post("/api/auth/users", json={"username": "waechter",
                                             "password": "passwort123",
                                             "role": "viewer"})
        # Viewer einloggen (Cookie des Admin überschreiben)
        client.cookies.clear()
        client.post("/api/auth/login", json={"username": "waechter",
                                             "password": "passwort123"})
        me = client.get("/api/auth/me").json()
        assert me["role"] == "viewer"
        # GET ok (Settings, History)
        assert client.get("/api/settings").status_code == 200
        assert client.get("/api/history?hours=1").status_code == 200
        # Schreiben 403 (Routen, die sonst 404/409 liefern würden)
        for method, path, payload in (
                ("post", "/api/instances", {"name": "T", "loader": "fabric",
                                            "game_version": "1.21.4"}),
                ("post", "/api/instances/xyz/start", None),
                ("post", "/api/modrinth/download", {"project_id": "p1"}),
                ("patch", "/api/instances/xyz", {"name": "Neu"}),
                ("delete", "/api/instances/xyz", None)):
            kwargs = {"json": payload} if payload is not None else {}
            resp = getattr(client, method)(path, **kwargs)
            assert resp.status_code == 403, (method, path, resp.status_code)

    def test_admin_cookie_post_ok(self, client):
        client.post("/api/auth/setup", json={"username": "boss",
                                             "password": "passwort123"})
        inst = client.post("/api/instances", json={"name": "T",
                                                   "loader": "fabric",
                                                   "game_version": "1.21.4",
                                                   "accept_eula": True})
        assert inst.status_code == 201

    def test_abgelaufene_session_401(self, client, monkeypatch):
        client.post("/api/auth/setup", json={"username": "boss",
                                             "password": "passwort123"})
        token = auth_mod.create_token("boss", "admin", ttl=10, now=1000.0)
        client.cookies.set(auth_mod.SESSION_COOKIE, token)
        fake_now = 1000.0 + 11
        real_verify = auth_mod.verify_token

        def pinned(token_arg, now=None):
            return real_verify(token_arg, now=fake_now)

        monkeypatch.setattr(auth_mod, "verify_token", pinned)
        r = client.get("/api/settings")
        assert r.status_code == 401


class TestBenutzerverwaltung:
    def test_erstellen_liste_patchen_loeschen(self, client):
        client.post("/api/auth/setup", json={"username": "boss",
                                             "password": "passwort123"})
        r = client.post("/api/auth/users", json={"username": "waechter",
                                                 "password": "passwort123",
                                                 "role": "viewer"})
        assert r.status_code == 201
        users = client.get("/api/auth/users").json()["users"]
        assert {u["username"] for u in users} == {"boss", "waechter"}
        assert all("salt" not in u and "scrypt_hash" not in u for u in users)
        # Rolle ändern
        r = client.patch("/api/auth/users/waechter", json={"role": "admin"})
        assert r.status_code == 200 and r.json()["role"] == "admin"
        # Passwort ändern (Admin-Reset)
        r = client.patch("/api/auth/users/waechter",
                         json={"password": "neues-pass-1"})
        assert r.status_code == 200
        client.cookies.clear()
        r = client.post("/api/auth/login", json={"username": "waechter",
                                                 "password": "neues-pass-1"})
        assert r.status_code == 200
        # Löschen
        client.cookies.clear()
        client.post("/api/auth/login", json={"username": "boss",
                                             "password": "passwort123"})
        assert client.delete("/api/auth/users/waechter").status_code == 200
        assert client.delete("/api/auth/users/waechter").status_code == 404

    def test_nur_admin_darf_verwalten(self, client):
        client.post("/api/auth/setup", json={"username": "boss",
                                             "password": "passwort123"})
        client.post("/api/auth/users", json={"username": "waechter",
                                             "password": "passwort123",
                                             "role": "viewer"})
        client.cookies.clear()
        client.post("/api/auth/login", json={"username": "waechter",
                                             "password": "passwort123"})
        # Eingeloggter Viewer: 403 (Rollen-Matrix wie im Guard)
        assert client.get("/api/auth/users").status_code == 403
        assert client.post("/api/auth/users", json={
            "username": "neu", "password": "passwort123"}).status_code == 403
        assert client.delete("/api/auth/users/boss").status_code == 403
        # Nicht eingeloggt: 401
        client.cookies.clear()
        assert client.get("/api/auth/users").status_code == 401

    def test_change_password_selbst(self, client):
        client.post("/api/auth/setup", json={"username": "boss",
                                             "password": "passwort123"})
        r = client.post("/api/auth/change-password", json={
            "current": "passwort123", "password": "neues-passwort-1"})
        assert r.status_code == 200
        client.cookies.clear()
        assert client.post("/api/auth/login", json={
            "username": "boss", "password": "neues-passwort-1"}).status_code == 200
        # Falsches aktuelles Passwort
        client.cookies.clear()
        client.post("/api/auth/login", json={"username": "boss",
                                             "password": "neues-passwort-1"})
        r = client.post("/api/auth/change-password", json={
            "current": "falsch-pass", "password": "noch-neuer-pass-1"})
        assert r.status_code == 400


class TestCookieFlags:
    def test_samesite_und_httponly(self, client):
        resp = client.post("/api/auth/setup",
                           json={"username": "boss", "password": "passwort123"})
        cookie = resp.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        assert "max-age=" in cookie


class TestHealthOffen:
    def test_health_bleibt_offen_mit_login(self, client):
        client.post("/api/auth/setup", json={"username": "boss",
                                             "password": "passwort123"})
        client.cookies.clear()
        assert client.get("/api/health").status_code == 200
