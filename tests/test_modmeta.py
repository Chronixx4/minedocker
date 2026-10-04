"""Tests für Mod-Metadaten aus der .jar, Problem-Erkennung und Mod-Papierkorb."""
import io
import json
import shutil
import time
import zipfile

import pytest
from fastapi import HTTPException

from app import instances, modmeta, modrinth
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen():
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    yield


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data if isinstance(data, bytes) else data.encode())
    return buf.getvalue()


def _fabric(mod_id, name=None, version="1.0.0", depends=None, env="*",
            jars=None, extra=None):
    obj = {"schemaVersion": 1, "id": mod_id, "name": name or mod_id,
           "version": version, "environment": env,
           "depends": depends or {}, "authors": ["Alice", {"name": "Bob"}],
           "description": "Eine  Test-Mod\n mit Umbruch"}
    if jars:
        obj["jars"] = [{"file": j} for j in jars]
    files = {"fabric.mod.json": json.dumps(obj)}
    files.update(extra or {})
    return _zip(files)


_FORGE_TOML = '''
modLoader="javafml"
loaderVersion="[47,)"
license="MIT"
[[mods]]
modId="create"
version="${file.jarVersion}"
displayName="Create"
authors="simibubi"
description="Bauen mit Zahnrädern"
[[dependencies.create]]
    modId="forge"
    mandatory=true
    versionRange="[47,)"
    side="BOTH"
[[dependencies.create]]
    modId="flywheel"
    mandatory=true
    versionRange="[0.6.10,)"
    side="CLIENT"
[[dependencies.create]]
    modId="registrate"
    mandatory=true
    versionRange="[1.3,)"
    side="BOTH"
[[dependencies.create]]
    modId="jei"
    mandatory=false
    side="BOTH"
'''

_NEOFORGE_TOML = '''
modLoader="javafml"
[[mods]]
modId="neomod"
version="2.1.0"
displayName="Neo Mod"
[[dependencies.neomod]]
    modId="geckolib"
    type="required"
    side="BOTH"
[[dependencies.neomod]]
    modId="jade"
    type="optional"
'''


def _create(loader="fabric", game_version="1.21.4"):
    return instances.create_instance(f"Mods-{loader}", loader, game_version,
                                     accept_eula=True)


def _put(inst, filename, data):
    path = instances.mods_dir(inst["id"]) / filename
    path.write_bytes(data)
    return path


class TestReadMeta:
    def test_fabric(self, tmp_path):
        p = tmp_path / "lithium.jar"
        p.write_bytes(_fabric("lithium", "Lithium", "0.15.1",
                              depends={"fabricloader": ">=0.16", "minecraft": "1.21.x",
                                       "fabric-api": ["*"]}))
        meta = modmeta.read_meta(p)
        assert meta["mod_id"] == "lithium"
        assert meta["name"] == "Lithium"
        assert meta["version"] == "0.15.1"
        assert meta["loaders"] == ["fabric"]
        assert meta["environment"] == "both"
        assert meta["authors"] == ["Alice", "Bob"]
        assert meta["description"] == "Eine Test-Mod mit Umbruch"
        assert {d["id"] for d in meta["depends"]} == {"fabricloader", "minecraft",
                                                     "fabric-api"}

    def test_fabric_client_umgebung(self, tmp_path):
        p = tmp_path / "iris.jar"
        p.write_bytes(_fabric("iris", env="client"))
        assert modmeta.read_meta(p)["environment"] == "client"

    def test_fabric_verschachtelte_jars_zaehlen_als_vorhanden(self, tmp_path):
        inner = _fabric("fabric-resource-loader-v0")
        p = tmp_path / "fabric-api.jar"
        p.write_bytes(_fabric("fabric-api", jars=["META-INF/jars/rl.jar"],
                              extra={"META-INF/jars/rl.jar": inner}))
        assert "fabric-resource-loader-v0" in modmeta.read_meta(p)["provides"]

    def test_forge_mit_jarversion_aus_manifest(self, tmp_path):
        p = tmp_path / "create.jar"
        p.write_bytes(_zip({
            "META-INF/mods.toml": _FORGE_TOML,
            "META-INF/MANIFEST.MF": "Manifest-Version: 1.0\nImplementation-Version: 0.5.1.f\n",
        }))
        meta = modmeta.read_meta(p)
        assert meta["mod_id"] == "create"
        assert meta["name"] == "Create"
        assert meta["version"] == "0.5.1.f"
        assert meta["loaders"] == ["forge"]
        assert meta["environment"] is None
        # nur Pflicht-Abhängigkeiten, die auf dem Server gelten
        assert [d["id"] for d in meta["depends"]] == ["forge", "registrate"]

    def test_neoforge_required_typ(self, tmp_path):
        p = tmp_path / "neo.jar"
        p.write_bytes(_zip({"META-INF/neoforge.mods.toml": _NEOFORGE_TOML}))
        meta = modmeta.read_meta(p)
        assert meta["loaders"] == ["neoforge"]
        assert [d["id"] for d in meta["depends"]] == ["geckolib"]

    def test_quilt(self, tmp_path):
        p = tmp_path / "q.jar"
        p.write_bytes(_zip({"quilt.mod.json": json.dumps({
            "quilt_loader": {"id": "qmod", "version": "3.0",
                             "metadata": {"name": "Q-Mod",
                                          "contributors": {"Carol": "Owner"}},
                             "depends": ["quilt_loader",
                                         {"id": "qsl", "versions": ">=6"},
                                         {"id": "opt", "optional": True}]},
            "minecraft": {"environment": "dedicated_server"}})}))
        meta = modmeta.read_meta(p)
        assert meta["name"] == "Q-Mod"
        assert meta["environment"] == "server"
        assert meta["authors"] == ["Carol"]
        assert [d["id"] for d in meta["depends"]] == ["quilt_loader", "qsl"]

    def test_kaputte_dateien_liefern_none(self, tmp_path):
        p = tmp_path / "kaputt.jar"
        p.write_bytes(b"kein zip")
        assert modmeta.read_meta(p) is None
        q = tmp_path / "ohne-meta.jar"
        q.write_bytes(_zip({"a.class": b"x"}))
        assert modmeta.read_meta(q) is None
        r = tmp_path / "falsches-json.jar"
        r.write_bytes(_zip({"fabric.mod.json": "{nicht json"}))
        assert modmeta.read_meta(r) is None


class TestAnalyse:
    def _types(self, overview):
        return sorted((p["filename"], p["type"]) for p in overview["problems"])

    def test_fehlende_und_deaktivierte_abhaengigkeit(self):
        inst = _create()
        _put(inst, "create.jar", _fabric("create", "Create",
                                         depends={"fabric-api": ">=0.92", "minecraft": "*"}))
        _put(inst, "needs-lib.jar", _fabric("needs-lib", depends={"cloth-config": "*"}))
        _put(inst, "cloth.jar.disabled", _fabric("cloth-config"))
        overview = instances.mods_overview(inst["id"])
        assert self._types(overview) == [("create.jar", "missing_dependency"),
                                         ("needs-lib.jar", "disabled_dependency")]
        create = next(m for m in overview["mods"] if m["filename"] == "create.jar")
        assert create["meta"]["name"] == "Create"
        assert "fabric-api (>=0.92)" in create["problems"][0]["message"]

    def test_abhaengigkeit_aus_verschachteltem_jar_ist_erfuellt(self):
        inst = _create()
        _put(inst, "fabric-api.jar", _fabric(
            "fabric-api", jars=["META-INF/jars/rl.jar"],
            extra={"META-INF/jars/rl.jar": _fabric("fabric-resource-loader-v0")}))
        _put(inst, "mod.jar", _fabric("mod", depends={
            "fabric-resource-loader-v0": "*", "fabric": "*"}))
        assert instances.mods_overview(inst["id"])["problems"] == []

    def test_falscher_loader_und_client_mod(self):
        inst = _create()
        _put(inst, "create.jar", _zip({"META-INF/mods.toml": _FORGE_TOML}))
        _put(inst, "iris.jar", _fabric("iris", "Iris", env="client"))
        overview = instances.mods_overview(inst["id"])
        assert self._types(overview) == [("create.jar", "wrong_loader"),
                                         ("iris.jar", "client_only")]

    def test_quilt_akzeptiert_fabric_und_neoforge_1201_forge(self):
        inst = _create("quilt")
        _put(inst, "a.jar", _fabric("a"))
        assert instances.mods_overview(inst["id"])["problems"] == []
        neo = _create("neoforge", "1.20.1")
        _put(neo, "create.jar", _zip({"META-INF/mods.toml": _FORGE_TOML}))
        _put(neo, "registrate.jar", _zip({"META-INF/mods.toml":
                                          '[[mods]]\nmodId="registrate"\nversion="1"\n'}))
        assert instances.mods_overview(neo["id"])["problems"] == []

    def test_doppelte_mod_id(self):
        inst = _create()
        _put(inst, "jei-1.jar", _fabric("jei", "JEI", "1"))
        _put(inst, "jei-2.jar", _fabric("jei", "JEI", "2"))
        assert self._types(instances.mods_overview(inst["id"])) == \
            [("jei-2.jar", "duplicate")]

    def test_deaktivierte_mods_haben_keine_probleme(self):
        inst = _create()
        _put(inst, "iris.jar.disabled", _fabric("iris", env="client",
                                                depends={"fehlt": "*"}))
        overview = instances.mods_overview(inst["id"])
        assert overview["problems"] == []
        assert overview["mods"][0]["meta"]["mod_id"] == "iris"

    def test_api_liefert_probleme_und_aenderungszeit(self, client):
        inst = _create()
        _put(inst, "iris.jar", _fabric("iris", env="client"))
        r = client.get(f"/api/instances/{inst['id']}/mods")
        assert r.status_code == 200
        data = r.json()
        assert data["problems"][0]["type"] == "client_only"
        assert isinstance(data["mods_changed_at"], int)
        detail = client.get(f"/api/instances/{inst['id']}").json()
        assert detail["mod_problems"][0]["type"] == "client_only"
        assert detail["mods"][0]["meta"]["mod_id"] == "iris"


class TestPapierkorb:
    def test_loeschen_und_wiederherstellen(self, client):
        inst = _create()
        path = _put(inst, "weg.jar", b"x")
        r = client.delete(f"/api/instances/{inst['id']}/mods/weg.jar")
        assert r.status_code == 200
        assert not path.exists()
        entries = client.get(f"/api/instances/{inst['id']}/mod-trash").json()["entries"]
        assert [(e["filename"], e["reason"]) for e in entries] == [("weg.jar", "deleted")]
        r = client.post(f"/api/instances/{inst['id']}/mod-trash/{entries[0]['id']}/restore")
        assert r.status_code == 200
        assert path.read_bytes() == b"x"
        assert client.get(f"/api/instances/{inst['id']}/mod-trash").json()["entries"] == []

    def test_wiederherstellen_ohne_ueberschreiben(self, client):
        inst = _create()
        _put(inst, "a.jar", b"alt")
        entry = instances.move_to_trash(inst["id"],
                                        instances.mods_dir(inst["id"]) / "a.jar", "deleted")
        _put(inst, "a.jar", b"neu")
        r = client.post(f"/api/instances/{inst['id']}/mod-trash/{entry}/restore")
        assert r.status_code == 409
        assert (instances.mods_dir(inst["id"]) / "a.jar").read_bytes() == b"neu"

    @pytest.mark.parametrize("entry", ["..", "x", "1234567890-0__deleted__..%2Fx.jar",
                                       "1234567890-0__evil__a.jar"])
    def test_ungueltige_eintraege(self, client, entry):
        inst = _create()
        r = client.post(f"/api/instances/{inst['id']}/mod-trash/{entry}/restore")
        # 405: Pfad mit Traversal erreicht die Route gar nicht erst
        assert r.status_code in (400, 404, 405)

    def test_eintrag_mit_unsicherem_namen_wird_nicht_wiederhergestellt(self):
        inst = _create()
        directory = instances.trash_dir(inst["id"])
        directory.mkdir(parents=True)
        name = f"{int(time.time())}-0__deleted__.versteckt.jar"
        (directory / name).write_bytes(b"x")
        with pytest.raises(HTTPException) as exc:
            instances.restore_from_trash(inst["id"], name)
        assert exc.value.status_code == 400

    def test_abgelaufene_eintraege_verschwinden(self):
        inst = _create()
        directory = instances.trash_dir(inst["id"])
        directory.mkdir(parents=True)
        old = int(time.time()) - (instances.TRASH_DAYS + 1) * 86400
        (directory / f"{old}-0__deleted__alt.jar").write_bytes(b"x")
        (directory / f"{int(time.time())}-0__deleted__neu.jar").write_bytes(b"x")
        assert [e["filename"] for e in instances.list_trash(inst["id"])] == ["neu.jar"]
        assert not (directory / f"{old}-0__deleted__alt.jar").exists()

    def test_leeren_und_instanz_loeschen(self, client):
        inst = _create()
        _put(inst, "a.jar", b"x")
        _put(inst, "b.jar", b"y")
        for name in ("a.jar", "b.jar"):
            instances.delete_mod(inst["id"], name)
        r = client.delete(f"/api/instances/{inst['id']}/mod-trash")
        assert r.json() == {"removed": 2}
        _put(inst, "c.jar", b"z")
        instances.delete_mod(inst["id"], "c.jar")
        instances.delete_instance(inst["id"])
        assert not instances.trash_dir(inst["id"]).exists()

    def test_papierkorb_liegt_ausserhalb_des_instanz_ordners(self):
        inst = _create()
        trash = instances.trash_dir(inst["id"]).resolve()
        assert instances.instance_dir(inst["id"]).resolve() not in trash.parents


class TestServerFilter:
    def test_server_ok_facette(self, monkeypatch):
        captured = {}

        class _Resp:
            status_code = 200

            def json(self):
                return {"hits": [], "total_hits": 0}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None, headers=None):
                captured.update(params or {})
                return _Resp()

        monkeypatch.setattr(modrinth.httpx, "AsyncClient", _Client)
        import asyncio
        asyncio.run(modrinth.search_mods("x", None, None, environment="server_ok"))
        facets = json.loads(captured["facets"])
        assert facets[-1] == ["server_side:required", "server_side:optional",
                              "server_side:unknown"]
