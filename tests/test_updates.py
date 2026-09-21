"""Tests für die Mod-Update-Prüfung: Murmur2-Fingerprint, Modrinth-SHA1-Lookup,
CurseForge-Fingerprint-Lookup und der 'Alles aktualisieren'-Job (gemocktes httpx)."""
import asyncio
import hashlib
import shutil

import httpx
import pytest
from fastapi import HTTPException

from app import instances, modrinth, updates
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen():
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    yield


@pytest.fixture()
def instanz():
    return instances.create_instance("Update-Srv", "fabric", "1.21.4",
                                     accept_eula=True)


def _patch_http(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _mods_dir(inst):
    return instances.instance_dir(inst["id"]) / "mods"


# ---------------------------------------------------------------------------
# Murmur2 (CurseForge-Fingerprint)
# ---------------------------------------------------------------------------

def _murmur2_reference(data: bytes) -> int:
    """Unverändert nach der Java-Referenz (CF-Doku, seed=1) übersetzt."""
    m = 0x5BD1E995
    length = len(data)
    h = (1 ^ length) & 0xFFFFFFFF

    def mix(k, h):
        k = (k * m) & 0xFFFFFFFF
        k ^= ((k & 0xFFFFFFFF) >> 24)  # Java '>>> 24'
        k = (k * m) & 0xFFFFFFFF
        h = (h * m) & 0xFFFFFFFF
        return h ^ k

    for i in range(length // 4):
        b = data[i * 4:i * 4 + 4]
        k = b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)
        h = mix(k, h)
    rest = length % 4
    if rest == 3:
        h ^= data[length - 3 + 2] << 16
    if rest >= 2:
        h ^= data[length - rest + 1] << 8
    if rest >= 1:
        h ^= data[length - rest]
        h = (h * m) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    h ^= h >> 15
    return h - 0x100000000 if h >= 0x80000000 else h


class TestMurmur2:
    def test_leerer_input_handgerechnet(self):
        # h = 1^0 = 1 → h ^= h>>13 → 1 → h *= m → 0x5BD1E995 → h ^= h>>15
        # 0x5BD1E995 ^ 0x0000B7A3 = 0x5BD15E36 = 1540447798
        assert updates.murmur2_cf(b"") == 1540447798

    def test_gleicht_mit_referenz(self):
        for data in (b"x", b"hallo welt", b"mod-datei-inhalt" * 37, b"a" * 1024):
            assert updates.murmur2_cf(data) == _murmur2_reference(data), data

    def test_signed_int32(self):
        result = _murmur2_reference(b"irgendein-mod-jar-inhalt" * 13)
        assert -2 ** 31 <= result <= 2 ** 31 - 1

    def test_hashes_for_fehlende_datei(self, tmp_path):
        assert updates.hashes_for(tmp_path / "fehlt.jar") == (None, None)


# ---------------------------------------------------------------------------
# Update-Check (Modrinth)
# ---------------------------------------------------------------------------

SHA_SODIUM = hashlib.sha1(b"sodium-mod").hexdigest()
SHA_CURRENT = hashlib.sha1(b"current-mod").hexdigest()
SHA_BETA = hashlib.sha1(b"beta-mod").hexdigest()
NEW_JAR = b"NEW-JAR-BYTES"                      # CDN-Inhalt der neuen Version
SHA_NEW_JAR = hashlib.sha1(NEW_JAR).hexdigest()

MR_VERSIONS = {
    SHA_SODIUM: [{
        "id": "ver-old", "project_id": "AAAA0000",
        "version_number": "0.15.0", "date_published": "2026-01-01T00:00:00Z",
        "game_versions": ["1.21.4"], "loaders": ["fabric"],
        "files": [{"filename": "sodium-fabric-0.15.0.jar", "primary": True,
                   "url": "https://cdn.modrinth.com/sodium-fabric-0.15.0.jar",
                   "size": 10, "hashes": {"sha1": SHA_SODIUM}}],
    }],
    SHA_CURRENT: [{
        "id": "ver-new", "project_id": "AAAA0000",
        "version_number": "0.16.0", "date_published": "2026-02-01T00:00:00Z",
        "game_versions": ["1.21.4"], "loaders": ["fabric"],
        "files": [{"filename": "sodium-fabric-0.16.0.jar", "primary": True,
                   "url": "https://cdn.modrinth.com/sodium-fabric-0.16.0.jar",
                   "size": 12, "hashes": {"sha1": SHA_CURRENT}}],
    }],
    SHA_BETA: [{
        "id": "ver-beta", "project_id": "BBBB0000",
        "version_number": "2.0.0-beta", "date_published": "2026-06-01T00:00:00Z",
        "game_versions": ["1.21.4"], "loaders": ["fabric"],
        "files": [{"filename": "beta-mod-2.0.0-beta.jar", "primary": True,
                   "url": "https://cdn.modrinth.com/beta.jar",
                   "size": 10, "hashes": {"sha1": SHA_BETA}}],
    }],
}

MR_LATEST = {
    "AAAA0000": [{
        "id": "ver-new", "project_id": "AAAA0000",
        "version_number": "0.16.0", "date_published": "2026-02-01T00:00:00Z",
        "game_versions": ["1.21.4"], "loaders": ["fabric"],
        "files": [{"filename": "sodium-fabric-0.16.0.jar", "primary": True,
                   "url": "https://cdn.modrinth.com/sodium-fabric-0.16.0.jar",
                   "size": 12, "hashes": {"sha1": SHA_NEW_JAR}}],
    }],
    "BBBB0000": [{
        "id": "ver-beta-old", "project_id": "BBBB0000",
        "version_number": "1.0.0", "date_published": "2026-01-01T00:00:00Z",
        "game_versions": ["1.21.4"], "loaders": ["fabric"],
        "files": [{"filename": "beta-mod-1.0.0.jar", "primary": True,
                   "url": "https://cdn.modrinth.com/beta-old.jar",
                   "size": 10, "hashes": {"sha1": SHA_BETA}}],
    }],
}


def _modrinth_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/sodium-fabric-0.16.0.jar"):
        return httpx.Response(200, content=b"NEW-JAR-BYTES")  # CDN-Download
    for sha1, versions in MR_VERSIONS.items():
        if path.endswith(f"/version_file/{sha1}"):
            return httpx.Response(200, json=versions)
    for project_id, versions in MR_LATEST.items():
        if path.endswith(f"/project/{project_id}/version"):
            return httpx.Response(200, json=versions)
    return httpx.Response(404, json={})


@pytest.fixture()
def _modrinth_mock(monkeypatch):
    _patch_http(monkeypatch, _modrinth_handler)


class TestCheckUpdatesModrinth:
    def test_update_verfuegbar_und_nicht_gefunden(self, instanz, _modrinth_mock):
        d = _mods_dir(instanz)
        (d / "sodium-fabric-0.15.0.jar").write_bytes(b"sodium-mod")
        (d / "unbekannt.jar").write_bytes(b"gibts-nicht")
        result = asyncio.run(updates.check_updates(instanz["id"]))
        by_name = {i["filename"]: i for i in result["items"]}
        assert by_name["sodium-fabric-0.15.0.jar"]["status"] == "update_available"
        assert by_name["sodium-fabric-0.15.0.jar"]["source"] == "modrinth"
        assert by_name["sodium-fabric-0.15.0.jar"]["installed"]["version_number"] == "0.15.0"
        assert by_name["sodium-fabric-0.15.0.jar"]["latest"]["version_number"] == "0.16.0"
        assert by_name["unbekannt.jar"]["status"] == "not_found"
        assert result["updatable"] == 1
        assert result["checked"] == 2

    def test_aktuell_und_newer_than_latest(self, instanz, _modrinth_mock):
        d = _mods_dir(instanz)
        (d / "sodium-fabric-0.16.0.jar").write_bytes(b"current-mod")
        (d / "beta-mod-2.0.0-beta.jar").write_bytes(b"beta-mod")
        result = asyncio.run(updates.check_updates(instanz["id"]))
        by_name = {i["filename"]: i for i in result["items"]}
        assert by_name["sodium-fabric-0.16.0.jar"]["status"] == "up_to_date"
        assert by_name["beta-mod-2.0.0-beta.jar"]["status"] == "newer_than_latest"
        assert result["updatable"] == 0

    def test_deaktivierte_mods_werden_geprüft(self, instanz, _modrinth_mock):
        d = _mods_dir(instanz)
        (d / "sodium-fabric-0.15.0.jar.disabled").write_bytes(b"sodium-mod")
        result = asyncio.run(updates.check_updates(instanz["id"]))
        item = result["items"][0]
        assert item["enabled"] is False
        assert item["status"] == "update_available"

    def test_leere_instanz(self, instanz, _modrinth_mock):
        result = asyncio.run(updates.check_updates(instanz["id"]))
        assert result == {"items": [], "checked": 0, "updatable": 0,
                          "curseforge_enabled": False}

    def test_anbieter_nicht_erreichbar_status_not_found(self, instanz, monkeypatch):
        def handler(request):
            return httpx.Response(500, json={})
        _patch_http(monkeypatch, handler)
        (_mods_dir(instanz) / "sodium.jar").write_bytes(b"sodium-mod")
        result = asyncio.run(updates.check_updates(instanz["id"]))
        assert result["items"][0]["status"] == "not_found"

    def test_unpassende_loader_version_wird_gefiltert(self, instanz, monkeypatch):
        """Version mit anderem Loader/MC-Version wird nicht als Treffer genutzt."""
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith(f"/version_file/{SHA_SODIUM}"):
                return httpx.Response(200, json=[{
                    "id": "ver-forge", "project_id": "CCCC0000",
                    "version_number": "9.9.9",
                    "game_versions": ["1.20.1"], "loaders": ["forge"],
                    "files": [{"filename": "x.jar", "primary": True,
                               "url": "https://cdn.modrinth.com/x.jar",
                               "size": 1, "hashes": {}}],
                }])
            return httpx.Response(404, json={})
        _patch_http(monkeypatch, handler)
        (_mods_dir(instanz) / "sodium.jar").write_bytes(b"sodium-mod")
        result = asyncio.run(updates.check_updates(instanz["id"]))
        # passender Eintrag fehlt → Fallback erster Eintrag, latest 404 → not_found
        assert result["items"][0]["status"] == "not_found"


# ---------------------------------------------------------------------------
# Update-Job (End-to-End über _run_update_job, da TestClient Hintergrund-
# Tasks abbricht — siehe PROJECT_MAP)
# ---------------------------------------------------------------------------

class TestUpdateJob:
    def _job(self, plan):
        return modrinth.create_job("test", len(plan), kind="update")

    def test_update_ersetzt_datei(self, instanz, _modrinth_mock):
        d = _mods_dir(instanz)
        (d / "sodium-fabric-0.15.0.jar").write_bytes(b"sodium-mod")
        check = asyncio.run(updates.check_updates(instanz["id"]))
        plan = updates._plan_from_check(check, None)
        assert len(plan) == 1
        job = self._job(plan)
        asyncio.run(updates._run_update_job(job, instanz, plan))
        assert job["status"] == "done"
        assert job["summary"]["updated"] == 1
        assert not (d / "sodium-fabric-0.15.0.jar").exists()
        assert (d / "sodium-fabric-0.16.0.jar").read_bytes() == \
            b"NEW-JAR-BYTES"

    def test_deaktivierter_zustand_bleibt(self, instanz, _modrinth_mock):
        d = _mods_dir(instanz)
        (d / "sodium-fabric-0.15.0.jar.disabled").write_bytes(b"sodium-mod")
        check = asyncio.run(updates.check_updates(instanz["id"]))
        plan = updates._plan_from_check(check, None)
        job = self._job(plan)
        asyncio.run(updates._run_update_job(job, instanz, plan))
        assert (d / "sodium-fabric-0.16.0.jar.disabled").is_file()
        assert not (d / "sodium-fabric-0.15.0.jar.disabled").exists()

    def test_neuer_than_latest_wird_übersprungen(self, instanz, _modrinth_mock):
        d = _mods_dir(instanz)
        (d / "beta-mod-2.0.0-beta.jar").write_bytes(b"beta-mod")
        check = asyncio.run(updates.check_updates(instanz["id"]))
        assert updates._plan_from_check(check, None) == []
        with pytest.raises(HTTPException):
            asyncio.run(updates.start_update(instanz["id"]))

    def test_kein_update_409(self, client, instanz, _modrinth_mock):
        r = client.post(f"/api/instances/{instanz['id']}/mods/update", json={})
        assert r.status_code == 409

    def test_filter_auf_einzelne_datei(self, instanz, _modrinth_mock):
        d = _mods_dir(instanz)
        (d / "sodium-fabric-0.15.0.jar").write_bytes(b"sodium-mod")
        check = asyncio.run(updates.check_updates(instanz["id"]))
        plan = updates._plan_from_check(check, ["andere.jar"])
        assert plan == []
        plan = updates._plan_from_check(check, ["SODIUM-FABRIC-0.15.0.jar"])
        assert len(plan) == 1  # Filter case-insensitive

    def test_download_fehler_landet_in_summary(self, instanz, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith(f"/version_file/{SHA_SODIUM}"):
                return httpx.Response(200, json=MR_VERSIONS[SHA_SODIUM])
            if path.endswith("/project/AAAA0000/version"):
                return httpx.Response(200, json=[{
                    "id": "ver-new", "version_number": "0.16.0",
                    "date_published": "2026-02-01T00:00:00Z",
                    "game_versions": ["1.21.4"], "loaders": ["fabric"],
                    "files": [{"filename": "sodium-fabric-0.16.0.jar",
                               "primary": True, "size": 10,
                               "url": "https://cdn.modrinth.com/boom.jar",
                               "hashes": {}}],
                }])
            if path.endswith("/boom.jar"):
                return httpx.Response(500)
            return httpx.Response(404, json={})
        _patch_http(monkeypatch, handler)
        d = _mods_dir(instanz)
        (d / "sodium-fabric-0.15.0.jar").write_bytes(b"sodium-mod")
        check = asyncio.run(updates.check_updates(instanz["id"]))
        plan = updates._plan_from_check(check, None)
        job = self._job(plan)
        asyncio.run(updates._run_update_job(job, instanz, plan))
        assert job["status"] == "done"
        assert job["summary"]["failed"] == 1
        assert job["summary"]["updated"] == 0
        # alte Datei blieb erhalten
        assert (d / "sodium-fabric-0.15.0.jar").is_file()
        assert not (d / "sodium-fabric-0.16.0.jar").exists()


# ---------------------------------------------------------------------------
# CurseForge-Fingerprint
# ---------------------------------------------------------------------------

CF_CONTENT = b"jei-mod-content"
CF_SHA = hashlib.sha1(CF_CONTENT).hexdigest()
CF_MURMUR = updates.murmur2_cf(CF_CONTENT)
JEI_NEW = b"JEI-NEW-BYTES"                      # CDN-Inhalt der neuen Version

CF_FILES = [
    {"id": 100, "modId": 555, "fileName": "jei-1.21.4-19.0.0.jar",
     "displayName": "Just Enough Items 19.0.0", "fileDate": "2026-01-01T00:00:00Z",
     "fileSize": 15, "releaseType": 1, "gameVersions": ["1.21.4", "Fabric"],
     "downloadUrl": "https://mediafile.forgecdn.net/jei-19.0.0.jar",
     "hashes": {"sha1": CF_SHA}},
    {"id": 101, "modId": 555, "fileName": "jei-1.21.4-19.1.0.jar",
     "displayName": "Just Enough Items 19.1.0", "fileDate": "2026-02-01T00:00:00Z",
     "fileSize": 16, "releaseType": 1, "gameVersions": ["1.21.4", "Fabric"],
     "downloadUrl": "https://mediafile.forgecdn.net/jei-19.1.0.jar",
     "hashes": {"sha1": hashlib.sha1(JEI_NEW).hexdigest()}},
]


def _curseforge_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.method == "POST" and path.endswith("/mods/fingerprints"):
        body = json_body(request)
        sent = body.get("fileFingerprints") or []
        if CF_MURMUR in sent:
            return httpx.Response(200, json={"data": {
                "exactMatches": [{"id": 100, "file": CF_FILES[0]}],
                "exactFingerprints": [CF_MURMUR],
            }})
        return httpx.Response(200, json={"data": {"exactMatches": []}})
    if path.endswith("/mods/555/files"):
        assert request.url.params.get("gameVersion") == "1.21.4"
        return httpx.Response(200, json={"data": CF_FILES})
    if path.endswith("/jei-19.1.0.jar"):
        return httpx.Response(200, content=b"JEI-NEW-BYTES")
    return httpx.Response(404, json={})


def json_body(request: httpx.Request) -> dict:
    import json
    try:
        return json.loads(request.content)
    except ValueError:
        return {}


@pytest.fixture()
def _curseforge_mock(monkeypatch):
    monkeypatch.setattr(settings, "cf_api_key", "test-key")
    _patch_http(monkeypatch, _curseforge_handler)


class TestCurseForge:
    def test_fingerprint_check_und_update(self, instanz, _curseforge_mock):
        d = _mods_dir(instanz)
        (d / "jei-1.21.4-19.0.0.jar").write_bytes(CF_CONTENT)
        result = asyncio.run(updates.check_updates(instanz["id"]))
        item = result["items"][0]
        assert item["source"] == "curseforge"
        assert item["status"] == "update_available"
        assert item["installed"]["file_id"] == "100"
        assert item["latest"]["file_id"] == "101"
        # Update-Job: neue Datei über CDN laden, alte entfernen
        plan = updates._plan_from_check(result, None)
        job = modrinth.create_job("test", len(plan), kind="update")
        asyncio.run(updates._run_update_job(job, instanz, plan))
        assert job["summary"]["updated"] == 1
        assert (d / "jei-1.21.4-19.1.0.jar").read_bytes() == b"JEI-NEW-BYTES"
        assert not (d / "jei-1.21.4-19.0.0.jar").exists()

    def test_ohne_key_wird_nicht_geprüft(self, instanz, _modrinth_mock):
        # CF-Key leer (fixture nicht aktiv) → jei nur über SHA1 suchbar → 404
        d = _mods_dir(instanz)
        (d / "jei-1.21.4-19.0.0.jar").write_bytes(CF_CONTENT)
        result = asyncio.run(updates.check_updates(instanz["id"]))
        assert result["items"][0]["status"] == "not_found"
        assert result["curseforge_enabled"] is False

    def test_cf_current(self, instanz, monkeypatch):
        """Installierte Datei ist bereits die neueste → up_to_date."""
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "POST" and path.endswith("/mods/fingerprints"):
                return httpx.Response(200, json={"data": {
                    "exactMatches": [{"id": 101, "file": CF_FILES[1]}],
                    "exactFingerprints": [CF_MURMUR],
                }})
            if path.endswith("/mods/555/files"):
                return httpx.Response(200, json={"data": CF_FILES})
            return httpx.Response(404, json={})
        monkeypatch.setattr(settings, "cf_api_key", "test-key")
        _patch_http(monkeypatch, handler)
        d = _mods_dir(instanz)
        (d / "jei-1.21.4-19.1.0.jar").write_bytes(CF_CONTENT)  # sha ist alt, CF matchet per murmur
        result = asyncio.run(updates.check_updates(instanz["id"]))
        assert result["items"][0]["status"] == "up_to_date"


# ---------------------------------------------------------------------------
# API (Job-Start; Hintergrund-Tasks laufen im TestClient nicht zu Ende)
# ---------------------------------------------------------------------------

class TestApi:
    def test_check_endpoint(self, client, instanz, _modrinth_mock):
        (_mods_dir(instanz) / "sodium-fabric-0.15.0.jar").write_bytes(b"sodium-mod")
        r = client.post(f"/api/instances/{instanz['id']}/mods/update-check")
        assert r.status_code == 200
        body = r.json()
        assert body["updatable"] == 1
        assert body["items"][0]["filename"] == "sodium-fabric-0.15.0.jar"

    def test_check_unbekannte_instanz_404(self, client):
        assert client.post("/api/instances/gibtsnicht/mods/update-check").status_code == 404

    def test_update_endpoint_startet_job(self, client, instanz, _modrinth_mock):
        (_mods_dir(instanz) / "sodium-fabric-0.15.0.jar").write_bytes(b"sodium-mod")
        r = client.post(f"/api/instances/{instanz['id']}/mods/update", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["job_id"]
        assert body["total"] == 1
        # Job ist abrufbar
        job = client.get(f"/api/jobs/{body['job_id']}").json()
        assert job["kind"] == "update"

    def test_update_unbekannte_instanz_404(self, client):
        assert client.post("/api/instances/gibtsnicht/mods/update",
                           json={}).status_code == 404
