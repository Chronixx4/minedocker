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
