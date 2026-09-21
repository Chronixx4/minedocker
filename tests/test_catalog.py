"""Tests für app.catalog (Mojang/Fabric/Forge/NeoForge/Quilt/Paper-Mocks)."""
import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app import catalog


def _patch_http(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _routes_handler(routes):
    def handle(request: httpx.Request) -> httpx.Response:
        for suffix, resp in routes.items():
            if request.url.path.endswith(suffix):
                return resp(request) if callable(resp) else resp
        return httpx.Response(404, json={})
    return handle


@pytest.fixture(autouse=True)
def _cache_leeren():
    catalog._CACHE.clear()
    yield
    catalog._CACHE.clear()


MOJANG = {
    "latest": {"release": "1.21.5"},
    "versions": [
        {"id": "1.21.5", "type": "release"},
        {"id": "1.21.4", "type": "release"},
        {"id": "25w14a", "type": "snapshot"},
        {"id": "1.20.1", "type": "release"},
    ],
}


class TestMcVersions:
    def test_releases_snapshots_und_cache(self, monkeypatch):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json=MOJANG)

        _patch_http(monkeypatch, handler)
        result = asyncio.run(catalog.mc_versions())
        assert result["latest_release"] == "1.21.5"
        assert result["releases"][0] == "1.21.5"
        assert "25w14a" in result["snapshots"]
        assert "25w14a" not in result["releases"]
        asyncio.run(catalog.mc_versions())  # zweiter Aufruf aus dem Cache
        assert calls["n"] == 1

    def test_server_fehler_502(self, monkeypatch):
        _patch_http(monkeypatch, lambda r: httpx.Response(503))
        with pytest.raises(HTTPException) as e:
            asyncio.run(catalog.mc_versions())
        assert e.value.status_code == 502

    def test_netzwerkfehler_502(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("boom", request=request)

        _patch_http(monkeypatch, handler)
        with pytest.raises(HTTPException) as e:
            asyncio.run(catalog.mc_versions())
        assert e.value.status_code == 502


class TestLoaders:
    def test_alle_sieben_loader_mit_versionen(self, monkeypatch):
        routes = {
            "v2/versions/loader/1.21.4": httpx.Response(200, json=[
                {"loader": {"version": "0.16.9", "stable": True}},
                {"loader": {"version": "0.16.5", "stable": False}},
            ]),
            "v3/versions/loader/1.21.4": httpx.Response(200, json=[
                {"loader": {"version": "0.26.0"}, "beta": False},
                {"loader": {"version": "0.25.0"}, "beta": True},
            ]),
            "promotions_slim.json": httpx.Response(200, json={
                "promos": {"1.21.4-latest": "51.0.26",
                           "1.21.4-recommended": "51.0.22"}}),
            "versions/releases/net/neoforged/neoforge": httpx.Response(200, json={
                "versions": ["21.4.100", "21.4.99", "20.9.1"]}),
            "projects/paper": httpx.Response(200, json={
                "versions": ["1.21.4", "1.20.6"]}),
        }
        _patch_http(monkeypatch, _routes_handler(routes))
        result = asyncio.run(catalog.loaders("1.21.4"))
        by_name = {e["loader"]: e for e in result["loaders"]}
        assert set(by_name) == set(catalog._META_LOADERS) | {"bukkit", "spigot"}

        assert by_name["fabric"]["versions"][0] == {"version": "0.16.9", "stable": True}
        assert by_name["fabric"]["error"] is None

        forge = by_name["forge"]["versions"]
        assert [v["version"] for v in forge] == ["51.0.22", "51.0.26"]  # recommended zuerst
        assert forge[0]["stable"] is True and forge[1]["stable"] is False

        assert [v["version"] for v in by_name["neoforge"]["versions"]] == ["21.4.100", "21.4.99"]

        quilt = by_name["quilt"]["versions"]
        assert quilt[0] == {"version": "0.26.0", "stable": True}
        assert quilt[1]["stable"] is False

        assert by_name["paper"]["versions"] == [{"version": "auto", "stable": True}]

        assert by_name["bukkit"]["versions"] == []
        assert "BuildTools" in by_name["bukkit"]["note"]
        assert "BuildTools" in by_name["spigot"]["note"]

    def test_unterstuetzte_mc_nicht_verfuegbar(self, monkeypatch):
        routes = {
            "v2/versions/loader/9.9.9": httpx.Response(200, json=[]),
            "v3/versions/loader/9.9.9": httpx.Response(200, json=[]),
            "promotions_slim.json": httpx.Response(200, json={"promos": {}}),
            "versions/releases/net/neoforged/neoforge": httpx.Response(200, json={
                "versions": []}),
            "projects/paper": httpx.Response(200, json={
                "project": "paper", "versions": {}}),
        }
        _patch_http(monkeypatch, _routes_handler(routes))
        result = asyncio.run(catalog.loaders("9.9.9"))
        by_name = {e["loader"]: e for e in result["loaders"]}
        assert by_name["fabric"]["versions"] == []
        assert by_name["forge"]["versions"] == []
        assert by_name["forge"]["note"] is not None
        assert by_name["paper"]["versions"] == []
        assert "nicht" in by_name["paper"]["note"]

    def test_ausfall_wird_nie_werfend(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("boom", request=request)

        _patch_http(monkeypatch, handler)
        result = asyncio.run(catalog.loaders("1.21.4"))
        assert len(result["loaders"]) == 7
        for entry in result["loaders"]:
            assert entry["versions"] == []
            if entry["loader"] in ("bukkit", "spigot"):
                # Keine HTTP-Requests — daher niemals ein Netzwerk-Fehler
                assert entry["error"] is None
            else:
                assert entry["error"] is not None

    def test_ungueltige_version_400(self):
        with pytest.raises(HTTPException) as e:
            asyncio.run(catalog.loaders("../evil"))
        assert e.value.status_code == 400


class TestNeoForgePrefix:
    @pytest.mark.parametrize("mc,expected", [
        ("1.21.1", "21.1"), ("1.21", "21"), ("1.20.1", "20.1"),
    ])
    def test_prefix(self, mc, expected):
        assert catalog._neoforge_prefix(mc) == expected
