"""API-Integrationstests über TestClient (conftest liefert client-Fixture)."""
import time

from app import modrinth
from app.config import settings


def _wait_job_done(job_id: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = modrinth.get_job(job_id)
        if job and job["status"] in ("done", "error"):
            return job
        time.sleep(0.05)
    return modrinth.get_job(job_id)


class TestGrundrouten:
    def test_health_ohne_auth(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_frontend_ausgeliefert(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_settings(self, client):
        resp = client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mc_version"] == "1.21.4"  # aus conftest-Env
        assert data["mod_loader"] == "fabric"
        assert data["auth_required"] is False

    def test_status_ressourcen(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        # Der Hauptserver wurde entfernt /api/status liefert nur noch
        # Docker-Ressourcen (Hauptserver-Status kommt von den Instanzen).
        assert set(data.keys()) == {"resources"}
        assert "found" in data["resources"]


class TestModsVerwaltung:
    def test_leere_liste(self, client):
        resp = client.get("/api/mods")
        assert resp.status_code == 200
        assert resp.json()["mods"] == []

    def test_mod_erscheint_in_liste(self, client):
        target = settings.mods_dir / "list-test.jar"
        settings.mods_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"jar-inhalt")
        try:
            resp = client.get("/api/mods")
            names = [m["filename"] for m in resp.json()["mods"]]
            assert "list-test.jar" in names
        finally:
            target.unlink(missing_ok=True)

    def test_delete_404(self, client):
        resp = client.delete("/api/mods/gibts-nicht.jar")
        assert resp.status_code == 404

    def test_delete_ungueltiger_name_400(self, client):
        resp = client.delete("/api/mods/evil.txt")
        assert resp.status_code == 400

    def test_delete_erfolgreich(self, client):
        settings.mods_dir.mkdir(parents=True, exist_ok=True)
        target = settings.mods_dir / "del-test.jar"
        target.write_bytes(b"x")
        resp = client.delete("/api/mods/del-test.jar")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "del-test.jar"
        assert not target.exists()


class TestModrinthSearch:
    def test_loader_whitelist(self, client):
        resp = client.get("/api/modrinth/search", params={"q": "x", "loader": "hack"})
        assert resp.status_code == 400

    def test_query_wird_gerettet_und_gekuerzt(self, client, monkeypatch):
        captured = {}

        async def fake_search(query, loader, game_version, limit=20, offset=0,
                              sort="relevance", environment=None):
            captured["query"] = query
            return {"total": 0, "hits": [], "loader": loader, "game_version": game_version}

        monkeypatch.setattr("app.modrinth.search_mods", fake_search)
        resp = client.get("/api/modrinth/search", params={"q": "a" * 150})
        assert resp.status_code == 200
        assert len(captured["query"]) == 100

    def test_offset_obergrenze(self, client, monkeypatch):
        captured = {}

        async def fake_search(query, loader, game_version, limit=20, offset=0,
                              sort="relevance", environment=None):
            captured["offset"] = offset
            return {"total": 0, "hits": [], "loader": loader, "game_version": game_version}

        monkeypatch.setattr("app.modrinth.search_mods", fake_search)
        resp = client.get("/api/modrinth/search", params={"offset": "99999"})
        assert resp.status_code == 200
        assert captured["offset"] == 1000

    def test_sort_whitelist(self, client):
        resp = client.get("/api/modrinth/search", params={"q": "x", "sort": "hacksort"})
        assert resp.status_code == 400

    def test_umgebung_unbekannt_400(self, client):
        resp = client.get("/api/modrinth/search", params={"q": "x", "environment": "beides"})
        assert resp.status_code == 400

    def test_any_filter_und_sort_werden_durchgereicht(self, client, monkeypatch):
        captured = {}

        async def fake_search(query, loader, game_version, limit=20, offset=0,
                              sort="relevance", environment=None):
            captured.update(loader=loader, game_version=game_version, sort=sort,
                            environment=environment)
            return {"total": 0, "hits": [], "loader": loader, "game_version": game_version}

        monkeypatch.setattr("app.modrinth.search_mods", fake_search)
        resp = client.get("/api/modrinth/search",
                          params={"q": "x", "loader": "any", "game_version": "any",
                                  "sort": "downloads", "environment": "server_required"})
        assert resp.status_code == 200
        assert captured["loader"] is None
        assert captured["game_version"] is None
        assert captured["sort"] == "downloads"
        assert captured["environment"] == "server_required"


class TestModrinthDownload:
    def _patch_resolve(self, monkeypatch, filename="test-mod.jar"):
        async def fake_resolve(project_id, version_id, loader, game_version):
            return filename, f"https://cdn.example/{filename}", 1234

        monkeypatch.setattr("app.modrinth.resolve_download", fake_resolve)

    def _patch_run(self, monkeypatch):
        async def fake_run(job, url, dest, **kwargs):
            job["status"] = "done"

        monkeypatch.setattr("app.modrinth.run_download_job", fake_run)

    def test_download_startet_job(self, client, monkeypatch):
        settings.mods_dir.mkdir(parents=True, exist_ok=True)
        target = settings.mods_dir / "test-mod.jar"
        target.unlink(missing_ok=True)
        self._patch_resolve(monkeypatch)
        self._patch_run(monkeypatch)
        try:
            resp = client.post("/api/modrinth/download",
                               json={"project_id": "p1", "version_id": None})
            assert resp.status_code == 200
            data = resp.json()
            assert data["filename"] == "test-mod.jar"
            assert data["size"] == 1234
            job = _wait_job_done(data["job_id"])
            assert job["status"] == "done"
        finally:
            target.unlink(missing_ok=True)

    def test_409_ohne_overwrite(self, client, monkeypatch):
        settings.mods_dir.mkdir(parents=True, exist_ok=True)
        target = settings.mods_dir / "test-mod.jar"
        target.write_bytes(b"alt")
        self._patch_resolve(monkeypatch)
        try:
            resp = client.post("/api/modrinth/download", json={"project_id": "p1"})
            assert resp.status_code == 409
            assert resp.json()["filename"] == "test-mod.jar"

            resp2 = client.post("/api/modrinth/download",
                                json={"project_id": "p1", "overwrite": True})
            assert resp2.status_code == 200  # Job gestartet
            _wait_job_done(resp2.json()["job_id"])
        finally:
            target.unlink(missing_ok=True)

    def test_unbekannter_job_404(self, client):
        resp = client.get("/api/modrinth/jobs/gibtsnicht")
        assert resp.status_code == 404

    def test_422_bei_schlechter_project_id(self, client):
        resp = client.post("/api/modrinth/download", json={"project_id": "a/b"})
        assert resp.status_code == 422
        assert "detail" in resp.json()


class TestApiKeyschutz:
    def test_401_ohne_key_wenn_gesetzt(self, client, monkeypatch):
        monkeypatch.setattr(settings, "api_key", "test-key-123")
        resp = client.get("/api/settings")
        assert resp.status_code == 401

    def test_200_mit_richtigem_key(self, client, monkeypatch):
        monkeypatch.setattr(settings, "api_key", "test-key-123")
        resp = client.get("/api/settings", headers={"X-API-Key": "test-key-123"})
        assert resp.status_code == 200

    def test_health_bleibt_offen(self, client, monkeypatch):
        monkeypatch.setattr(settings, "api_key", "test-key-123")
        resp = client.get("/api/health")
        assert resp.status_code == 200


class TestCors:
    def test_preflight_header(self, client):
        resp = client.get("/api/health", headers={"Origin": "http://example.com"})
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "*"
