"""Tests für das schnelle Öffnen eines Servers: Speicher-Zählung mit Cache
und Nachladen, Ping nur bei laufendem Container, Zeitmessung und der auf
Platte gespiegelte Mod-Metadaten-Cache."""
import asyncio
import json
import shutil
import threading
import time
import uuid
import zipfile

import pytest
from fastapi import Response

from app import instances, main, modmeta, runtime
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen():
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    instances.invalidate_disk_cache()
    yield
    instances.invalidate_disk_cache()


def _create(name=None):
    return instances.create_instance(name or f"Perf-{uuid.uuid4().hex[:8]}",
                                     "fabric", "1.21.4", accept_eula=True)


def _seed_world(instance_id):
    d = instances.instance_dir(instance_id)
    (d / "world" / "region").mkdir(parents=True, exist_ok=True)
    (d / "world" / "level.dat").write_bytes(b"L" * 8)
    (d / "world" / "region" / "r.0.0.mca").write_bytes(b"x" * 100)


class TestDiskCache:
    def test_parallele_aufrufe_zaehlen_nur_einmal(self, monkeypatch):
        inst = _create()
        _seed_world(inst["id"])
        calls = []
        original = instances._measure_disk

        def slow(instance_id):
            calls.append(instance_id)
            time.sleep(0.2)
            return original(instance_id)

        monkeypatch.setattr(instances, "_measure_disk", slow)
        results = []
        threads = [threading.Thread(
            target=lambda: results.append(instances.disk_usage(inst["id"])))
            for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(calls) == 1
        assert all(r["world_bytes"] == 108 for r in results)

    def test_veralteter_wert_bleibt_sichtbar(self, monkeypatch):
        inst = _create()
        _seed_world(inst["id"])
        instances.disk_usage(inst["id"])
        cached, fresh = instances.disk_usage_cached(inst["id"])
        assert fresh and cached["world_bytes"] == 108
        monkeypatch.setattr(instances, "_DISK_CACHE_TTL", 0.0)
        cached, fresh = instances.disk_usage_cached(inst["id"])
        assert cached["world_bytes"] == 108 and not fresh

    def test_invalidierung_waehrend_zaehlung_wird_nicht_gespeichert(self, monkeypatch):
        inst = _create()
        original = instances._measure_disk

        def measure_and_invalidate(instance_id):
            result = original(instance_id)
            instances.invalidate_disk_cache(instance_id)  # z. B. Upload parallel
            return result

        monkeypatch.setattr(instances, "_measure_disk", measure_and_invalidate)
        instances.disk_usage(inst["id"])
        assert instances.disk_usage_cached(inst["id"]) == (None, False)


class TestDetailSchnell:
    def test_langsame_zaehlung_blockiert_details_nicht(self, client, monkeypatch):
        inst = _create()
        _seed_world(inst["id"])
        original = instances._measure_disk

        def slow(instance_id):
            time.sleep(0.5)
            return original(instance_id)

        monkeypatch.setattr(instances, "_measure_disk", slow)
        monkeypatch.setattr(main, "_DISK_BUDGET_SECONDS", 0.05)

        async def detail_dauer():
            started = time.monotonic()
            body = await main.instance_detail(inst["id"], Response())
            return body, time.monotonic() - started

        # Direkt im Event-Loop messen: der TestClient wartet beim Beenden
        # seines Loops auf den Hintergrund-Thread und verfälscht die Zeit
        body, dauer = asyncio.run(detail_dauer())
        assert dauer < 0.4
        assert body["disk"] is None and body["disk_pending"] is True
        # Nachladen liefert den echten Wert
        r = client.get(f"/api/instances/{inst['id']}/disk")
        assert r.status_code == 200 and r.json()["world_bytes"] == 108

    def test_schnelle_zaehlung_direkt_in_details(self, client):
        inst = _create()
        _seed_world(inst["id"])
        body = client.get(f"/api/instances/{inst['id']}").json()
        assert body["disk_pending"] is False
        assert body["disk"]["world_bytes"] == 108

    def test_kein_ping_bei_gestopptem_container(self, client, monkeypatch):
        inst = _create()
        pings = []
        monkeypatch.setattr(main, "server_status",
                            lambda *a, **k: pings.append(a) or {"online": True})
        monkeypatch.setattr(runtime, "container_status", lambda i: {
            "running": False, "container": "exited", "started_at": None, "error": None})
        body = client.get(f"/api/instances/{inst['id']}").json()
        assert pings == []
        assert body["ping"]["online"] is False

    def test_ping_bei_laufendem_container(self, client, monkeypatch):
        inst = _create()
        monkeypatch.setattr(main, "server_status", lambda *a, **k: {
            "online": True, "version": "1.21.4", "motd": "hi",
            "players": {"online": 1, "max": 20, "sample": []}, "error": None})
        monkeypatch.setattr(runtime, "container_status", lambda i: {
            "running": True, "container": "running", "started_at": None, "error": None})
        body = client.get(f"/api/instances/{inst['id']}").json()
        assert body["ping"]["online"] is True

    def test_server_timing_header(self, client):
        inst = _create()
        r = client.get(f"/api/instances/{inst['id']}")
        timing = r.headers["Server-Timing"]
        for part in ("docker;dur=", "mods;dur=", "disk;dur=", "app;dur="):
            assert part in timing
        assert "app;dur=" in client.get("/api/health").headers["Server-Timing"]

    def test_disk_unbekannte_instanz_404(self, client):
        assert client.get("/api/instances/deadbeef/disk").status_code == 404


class TestModmetaPlattenCache:
    @pytest.fixture(autouse=True)
    def _frischer_cache(self, monkeypatch):
        monkeypatch.setattr(modmeta, "_CACHE", {})
        monkeypatch.setattr(modmeta, "_CACHE_STATE", {"loaded": False, "dirty": False})
        modmeta._cache_path().unlink(missing_ok=True)
        yield
        modmeta._cache_path().unlink(missing_ok=True)

    def _jar(self, path, mod_id):
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("fabric.mod.json", json.dumps(
                {"schemaVersion": 1, "id": mod_id, "version": "1.0"}))

    def test_cache_ueberlebt_neustart(self, monkeypatch):
        inst = _create()
        mods = instances.mods_dir(inst["id"])
        mods.mkdir(parents=True, exist_ok=True)
        self._jar(mods / "a.jar", "alpha")
        instances.mods_overview(inst["id"])
        assert modmeta._cache_path().is_file()

        # „Neustart“: Speicher-Cache leer, .jar darf nicht erneut geöffnet werden
        monkeypatch.setattr(modmeta, "_CACHE", {})
        monkeypatch.setattr(modmeta, "_CACHE_STATE", {"loaded": False, "dirty": False})
        monkeypatch.setattr(modmeta, "_read", lambda p: pytest.fail("jar neu gelesen"))
        overview = instances.mods_overview(inst["id"])
        assert overview["mods"][0]["meta"]["mod_id"] == "alpha"

    def test_geaenderte_datei_wird_neu_gelesen(self):
        inst = _create()
        mods = instances.mods_dir(inst["id"])
        mods.mkdir(parents=True, exist_ok=True)
        self._jar(mods / "a.jar", "alpha")
        instances.mods_overview(inst["id"])
        time.sleep(0.01)
        self._jar(mods / "a.jar", "beta")
        overview = instances.mods_overview(inst["id"])
        assert overview["mods"][0]["meta"]["mod_id"] == "beta"

    def test_kaputte_cache_datei_wird_ignoriert(self):
        modmeta._cache_path().write_text("{kaputt", encoding="utf-8")
        inst = _create()
        mods = instances.mods_dir(inst["id"])
        mods.mkdir(parents=True, exist_ok=True)
        self._jar(mods / "a.jar", "alpha")
        assert instances.mods_overview(inst["id"])["mods"][0]["meta"]["mod_id"] == "alpha"
