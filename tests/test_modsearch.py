"""Tests für den Modbrowser: Kategorie-Filter, Such-Cache mit Keep-Alive-Client
und die Installiert-Markierung per Modrinth-Batch-Lookup (gemocktes httpx)."""
import asyncio
import hashlib
import json
import shutil
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import HTTPException

from app import curseforge, instances, modcategories, modrinth, updates
from app.config import settings


def _patch_http(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.fixture()
def instanz():
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    updates.invalidate_installed_cache()
    inst = instances.create_instance("Browser-Srv", "fabric", "1.21.4", accept_eula=True)
    yield inst
    updates.invalidate_installed_cache()


class TestKategorien:
    def test_modrinth_kategorie_als_facet(self, monkeypatch):
        captured = {}

        def handler(request):
            captured["query"] = parse_qs(urlparse(str(request.url)).query)
            return httpx.Response(200, json={"total_hits": 0, "hits": []})

        _patch_http(monkeypatch, handler)
        asyncio.run(modrinth.search_mods("", "fabric", "1.21.4", category="worldgen"))
        facets = json.loads(captured["query"]["facets"][0])
        assert ["categories:worldgen"] in facets
        assert ["categories:fabric"] in facets  # Loader bleibt eigene UND-Gruppe

    def test_modrinth_kategorie_validiert(self):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(modrinth.search_mods("", None, None, category="a b\"]"))
        assert exc.value.status_code == 400

    def test_curseforge_kategorie_als_category_id(self, monkeypatch):
        monkeypatch.setattr(settings, "cf_api_key", "k")
        captured = {}

        def handler(request):
            captured["query"] = parse_qs(urlparse(str(request.url)).query)
            return httpx.Response(200, json={"data": [], "pagination": {"totalCount": 0}})

        _patch_http(monkeypatch, handler)
        asyncio.run(curseforge.search_mods("", "fabric", "1.21.4", category="419"))
        assert captured["query"]["categoryId"] == ["419"]

    def test_curseforge_kategorie_nur_numerisch(self, monkeypatch):
        monkeypatch.setattr(settings, "cf_api_key", "k")
        with pytest.raises(HTTPException):
            asyncio.run(curseforge.search_mods("", None, None, category="magic"))

    def test_modrinth_liste_nur_mod_kategorien_deutsch(self, monkeypatch):
        tags = [
            {"name": "worldgen", "project_type": "mod", "header": "categories"},
            {"name": "magic", "project_type": "mod", "header": "categories"},
            {"name": "brandneu", "project_type": "mod", "header": "categories"},
            {"name": "16x", "project_type": "resourcepack", "header": "resolutions"},
            {"name": "magic", "project_type": "modpack", "header": "categories"},
        ]
        _patch_http(monkeypatch, lambda r: httpx.Response(200, json=tags))
        cats = asyncio.run(modcategories.categories("modrinth"))
        assert {"id": "worldgen", "name": "Weltgenerierung"} in cats
        assert {"id": "magic", "name": "Magie"} in cats
        assert {"id": "brandneu", "name": "Brandneu"} in cats  # unbekannt → Anbietername
        assert len(cats) == 3

    def test_modrinth_ausfall_liefert_eingebaute_liste(self, monkeypatch):
        _patch_http(monkeypatch, lambda r: httpx.Response(503))
        cats = asyncio.run(modcategories.categories("modrinth"))
        assert {"id": "technology", "name": "Technik"} in cats

    def test_curseforge_liste_mit_oberkategorie(self, monkeypatch):
        monkeypatch.setattr(settings, "cf_api_key", "k")
        data = {"data": [
            {"id": 6, "name": "Mods", "slug": "mc-mods", "isClass": True},
            {"id": 412, "name": "Technology", "slug": "technology", "parentCategoryId": 6},
            {"id": 417, "name": "Energy", "slug": "technology-energy", "parentCategoryId": 412},
            {"id": 419, "name": "Magic", "slug": "magic", "parentCategoryId": 6},
        ]}
        _patch_http(monkeypatch, lambda r: httpx.Response(200, json=data))
        cats = asyncio.run(modcategories.categories("curseforge"))
        names = {c["id"]: c["name"] for c in cats}
        assert names == {"412": "Technik", "417": "Technik: Energie", "419": "Magie"}

    def test_curseforge_ohne_key_leer(self, monkeypatch):
        monkeypatch.setattr(settings, "cf_api_key", "")
        assert asyncio.run(modcategories.categories("curseforge")) == []

    def test_endpunkt(self, client, monkeypatch):
        _patch_http(monkeypatch, lambda r: httpx.Response(200, json=[
            {"name": "optimization", "project_type": "mod", "header": "categories"}]))
        resp = client.get("/api/mods/categories", params={"source": "modrinth"})
        assert resp.status_code == 200
        assert resp.json()["categories"] == [{"id": "optimization", "name": "Optimierung"}]
        assert client.get("/api/mods/categories", params={"source": "x"}).status_code == 400


class TestSuchCache:
    def test_gleiche_seite_nur_einmal_abgefragt(self, monkeypatch):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, json={"total_hits": 1, "hits": [
                {"project_id": "AAA", "title": "A"}]})

        _patch_http(monkeypatch, handler)

        async def run():
            first = await modrinth.search_mods("x", "fabric", "1.21.4", offset=20)
            first["hits"][0]["installed"] = True  # Aufrufer verändert Treffer
            second = await modrinth.search_mods("x", "fabric", "1.21.4", offset=20)
            other = await modrinth.search_mods("x", "fabric", "1.21.4", offset=40)
            return second, other

        second, _other = asyncio.run(run())
        assert len(calls) == 2  # offset 20 einmal, offset 40 einmal
        assert "installed" not in second["hits"][0]  # Cache liefert Kopien


class TestInstalliertMarkierung:
    def test_batch_lookup_markiert_treffer(self, client, instanz, monkeypatch):
        mods = instances.mods_dir(instanz["id"])
        mods.mkdir(parents=True, exist_ok=True)
        (mods / "sodium.jar").write_bytes(b"sodium-bytes")
        (mods / "lithium.jar").write_bytes(b"lithium-bytes")
        sha_sodium = hashlib.sha1(b"sodium-bytes").hexdigest()
        seen = {}

        def handler(request):
            path = request.url.path
            if request.method == "POST" and path.endswith("/version_files"):
                body = json.loads(request.content)
                seen["hashes"] = sorted(body["hashes"])
                assert body["algorithm"] == "sha1"
                return httpx.Response(200, json={
                    sha_sodium: {"id": "v1", "project_id": "AANobbMI"}})
            if path.endswith("/search"):
                return httpx.Response(200, json={"total_hits": 2, "hits": [
                    {"project_id": "AANobbMI", "title": "Sodium"},
                    {"project_id": "gvQqBUqZ", "title": "Lithium"}]})
            return httpx.Response(404, json={})

        _patch_http(monkeypatch, handler)
        resp = client.get("/api/modrinth/search",
                          params={"instance_id": instanz["id"], "loader": "fabric",
                                  "game_version": "1.21.4"})
        assert resp.status_code == 200
        hits = {h["title"]: h["installed"] for h in resp.json()["hits"]}
        assert hits == {"Sodium": True, "Lithium": False}
        assert len(seen["hashes"]) == 2  # beide Dateien in EINER Anfrage

    def test_ungueltige_instanz_400(self, client):
        resp = client.get("/api/modrinth/search", params={"instance_id": "../x"})
        assert resp.status_code == 400
