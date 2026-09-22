"""Tests für den Datei-Browser je Instanz (app/filebrowser.py + Routen)."""
import shutil

import pytest
from fastapi import HTTPException

from app import filebrowser as fb
from app import instances
from app.config import settings


@pytest.fixture(autouse=True)
def _leere_instanzen():
    settings.instances_dir.mkdir(parents=True, exist_ok=True)
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    yield
    for entry in settings.instances_dir.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


@pytest.fixture()
def instanz():
    return instances.create_instance("Browser-Srv", "fabric", "1.21.4",
                                     accept_eula=True)


@pytest.fixture()
def basis(instanz):
    return instances.instance_dir(instanz["id"])


# ---------------------------------------------------------------------------
# Pfad-Normalisierung / Traversal-Schutz
# ---------------------------------------------------------------------------

class TestPfade:
    def test_normalize(self):
        assert fb.normalize_rel_path("") == ""
        assert fb.normalize_rel_path(".") == ""
        assert fb.normalize_rel_path("logs//x.txt") == "logs/x.txt"
        assert fb.normalize_rel_path("a/./b") == "a/b"
        assert fb.normalize_rel_path("unter\\ordner") == "unter/ordner"

    @pytest.mark.parametrize("bad", ("../x", "a/../../b", "..", "/absolut",
                                     "C:\\x", "a\x00b"))
    def test_ungueltig(self, bad):
        with pytest.raises(HTTPException) as e:
            fb.normalize_rel_path(bad)
        assert e.value.status_code == 400

    def test_resolve_haelt_im_instanz_Ordner(self, instanz):
        path, rel = fb.resolve_path(instanz["id"], "mods/sub/mod.jar")
        assert path == (instances.instance_dir(instanz["id"])
                        / "mods/sub/mod.jar").resolve()
        assert rel == "mods/sub/mod.jar"
        with pytest.raises(HTTPException):
            fb.resolve_path(instanz["id"], "../../etc/passwd")

    def test_symlink_escape_blockiert(self, instanz, basis, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("geheim", encoding="utf-8")
        try:
            (basis / "link.txt").symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks auf diesem System nicht verfügbar")
        with pytest.raises(HTTPException):
            fb.read_text(instanz["id"], "link.txt")

    def test_zu_langer_pfad(self, instanz):
        with pytest.raises(HTTPException):
            fb.resolve_path(instanz["id"], "a" * 600)


# ---------------------------------------------------------------------------
# Geschützte Ziele
# ---------------------------------------------------------------------------

class TestSchutz:
    def test_verwaltete_dateien_geschlossen(self, instanz, basis):
        (basis / "server.properties").write_text("server-port=25565",
                                                 encoding="utf-8")
        (basis / "instance.json").write_text("{}", encoding="utf-8")
        for rel in ("server.properties", "instance.json"):
            with pytest.raises(HTTPException) as e:
                fb.read_text(instanz["id"], rel)
            assert e.value.status_code == 400
            with pytest.raises(HTTPException):
                fb.write_text(instanz["id"], rel, "x")
            with pytest.raises(HTTPException):
                fb.delete(instanz["id"], rel)
            with pytest.raises(HTTPException):
                fb.rename(instanz["id"], rel, "kopie.txt")

    def test_packs_nur_lesen(self, instanz, basis):
        packs_dir = basis / "packs"
        packs_dir.mkdir(exist_ok=True)
        (packs_dir / "pack.zip").write_bytes(b"PK")
        # Lesen erlaubt (Download-Pfad)
        path, _rel = fb.download_path(instanz["id"], "packs/pack.zip")
        assert path.is_file()
        # Schreiben blockiert
        with pytest.raises(HTTPException) as e:
            fb.write_text(instanz["id"], "packs/pack.zip", "x")
        assert e.value.status_code == 400
        with pytest.raises(HTTPException):
            fb.delete(instanz["id"], "packs/pack.zip")
        with pytest.raises(HTTPException):
            fb.rename(instanz["id"], "packs/pack.zip", "packs/p2.zip")
        with pytest.raises(HTTPException):
            fb.upload_dest(instanz["id"], "packs/neu.zip", False)

    def test_rename_in_geschuetztes_ziel_blockiert(self, instanz, basis):
        (basis / "a.txt").write_text("x", encoding="utf-8")
        with pytest.raises(HTTPException):
            fb.rename(instanz["id"], "a.txt", "server.properties")


# ---------------------------------------------------------------------------
# Liste / Text-Editor
# ---------------------------------------------------------------------------

class TestListeUndEditor:
    def test_liste_root(self, instanz, basis):
        (basis / "logs").mkdir()
        (basis / "logs" / "latest.log").write_text("hallo", encoding="utf-8")
        result = fb.list_dir(instanz["id"], "")
        assert result["path"] == ""
        names = [e["name"] for e in result["entries"]]
        assert "instance.json" in names and "logs" in names
        # Ordner zuerst
        assert result["entries"][0]["type"] == "dir"
        entry = next(e for e in result["entries"] if e["name"] == "instance.json")
        assert entry["managed"] is True

    def test_liste_unterordner_und_404(self, instanz, basis):
        (basis / "logs").mkdir()
        out = fb.list_dir(instanz["id"], "logs")
        assert out["path"] == "logs"
        with pytest.raises(HTTPException) as e:
            fb.list_dir(instanz["id"], "gibtsnicht")
        assert e.value.status_code == 404

    def test_text_roundtrip_und_atomar(self, instanz, basis):
        fb.mkdir(instanz["id"], "logs")
        fb.write_text(instanz["id"], "logs/start.txt", "erste Zeile\n")
        assert (basis / "logs" / "start.txt").read_text(encoding="utf-8") == "erste Zeile\n"
        assert not (basis / "logs" / "start.txt.tmp").exists()
        text = fb.read_text(instanz["id"], "logs/start.txt")
        assert text["content"] == "erste Zeile\n"

    def test_binärdatei_erkannt(self, instanz, basis):
        (basis / "bild.png").write_bytes(b"PNG\x00\x01\x02")
        with pytest.raises(HTTPException) as e:
            fb.read_text(instanz["id"], "bild.png")
        assert e.value.status_code == 400
        assert "Binärdatei" in e.value.detail

    def test_zu_grosse_textdatei(self, instanz, basis):
        big = b"a" * (fb.TEXT_LIMIT + 1)
        (basis / "gross.txt").write_bytes(big)
        with pytest.raises(HTTPException) as e:
            fb.read_text(instanz["id"], "gross.txt")
        assert e.value.status_code == 400
        # Schreiben ebenfalls gedeckelt
        with pytest.raises(HTTPException):
            fb.write_text(instanz["id"], "gross.txt", "x" * (fb.TEXT_LIMIT + 1))

    def test_lesen_ohne_datei_404(self, instanz, basis):
        with pytest.raises(HTTPException) as e:
            fb.read_text(instanz["id"], "nix.txt")
        assert e.value.status_code == 404


# ---------------------------------------------------------------------------
# Upload / mkdir / rename / delete
# ---------------------------------------------------------------------------

class TestUpload:
    def test_dest_409_und_overwrite(self, instanz, basis):
        (basis / "vorhanden.zip").write_bytes(b"alt")
        with pytest.raises(HTTPException) as e:
            fb.upload_dest(instanz["id"], "vorhanden.zip", False)
        assert e.value.status_code == 409
        assert fb.upload_dest(instanz["id"], "vorhanden.zip", True).is_file()
        with pytest.raises(HTTPException):
            fb.upload_dest(instanz["id"], "fehlt/unter/ordner.zip", False)


class TestMkdirRenameDelete:
    def test_mkdir(self, instanz, basis):
        assert fb.mkdir(instanz["id"], "configs")["created"] == "configs"
        assert fb.mkdir(instanz["id"], "configs/sub")["created"] == "configs/sub"
        assert (basis / "configs" / "sub").is_dir()
        with pytest.raises(HTTPException) as e:
            fb.mkdir(instanz["id"], "configs/sub")
        assert e.value.status_code == 409
        with pytest.raises(HTTPException) as e:
            fb.mkdir(instanz["id"], "garnicht/da/xyz")
        assert e.value.status_code == 400

    def test_rename(self, instanz, basis):
        fb.write_text(instanz["id"], "alt.txt", "inhalt")
        result = fb.rename(instanz["id"], "alt.txt", "neu.txt")
        assert result == {"renamed": "alt.txt", "to": "neu.txt"}
        assert (basis / "neu.txt").is_file()
        with pytest.raises(HTTPException) as e:
            fb.rename(instanz["id"], "neu.txt", "gibts/nicht/ordner/x.txt")
        assert e.value.status_code == 400

    def test_delete_datei_und_leerer_ordner(self, instanz, basis):
        fb.write_text(instanz["id"], "weg.txt", "x")
        (basis / "leer").mkdir()
        (basis / "voll").mkdir()
        (basis / "voll" / "x.txt").write_text("x", encoding="utf-8")
        assert fb.delete(instanz["id"], "weg.txt")["deleted"] == "weg.txt"
        assert fb.delete(instanz["id"], "leer")["deleted"] == "leer"
        with pytest.raises(HTTPException) as e:
            fb.delete(instanz["id"], "voll")
        assert e.value.status_code == 409
        with pytest.raises(HTTPException) as e:
            fb.delete(instanz["id"], "")
        assert e.value.status_code == 400
        with pytest.raises(HTTPException) as e:
            fb.delete(instanz["id"], "nichtda")
        assert e.value.status_code == 404


# ---------------------------------------------------------------------------
# API-Routen (TestClient)
# ---------------------------------------------------------------------------

class TestRouten:
    def test_crud_ueber_api(self, client, instanz, basis):
        base = f"/api/instances/{instanz['id']}/files"
        # mkdir + Liste
        r = client.post(f"{base}/mkdir", json={"path": "schriften"})
        assert r.status_code == 200
        # Text speichern (PUT)
        r = client.put(f"{base}/content",
                       json={"path": "schriften/notiz.txt", "content": "Hallo"})
        assert r.status_code == 200
        # Lesen (GET)
        r = client.get(f"{base}/content", params={"path": "schriften/notiz.txt"})
        assert r.status_code == 200
        assert r.json()["content"] == "Hallo"
        # Rename
        r = client.post(f"{base}/rename",
                        json={"from_path": "schriften/notiz.txt",
                              "to_path": "notiz.txt"})
        assert r.status_code == 200
        # Download
        r = client.get(f"{base}/download", params={"path": "notiz.txt"})
        assert r.status_code == 200
        assert b"Hallo" in r.content
        # Löschen
        r = client.delete(f"{base}", params={"path": "notiz.txt"})
        assert r.status_code == 200
        # Unbekannte Instanz → 404
        assert client.get("/api/instances/gibtsnicht/files").status_code == 404

    def test_upload_routine(self, client, instanz, basis):
        base = f"/api/instances/{instanz['id']}/files"
        client.post(f"{base}/mkdir", json={"path": "uploads"})
        r = client.post(f"{base}/upload",
                        data={"path": "uploads/box.jar", "overwrite": "false"},
                        files={"file": ("box.jar", b"JAR-DATEN", "application/java-archive")})
        assert r.status_code == 200
        assert (basis / "uploads" / "box.jar").read_bytes() == b"JAR-DATEN"
        # Ohne overwrite → 409
        r = client.post(f"{base}/upload",
                        data={"path": "uploads/box.jar"},
                        files={"file": ("box.jar", b"NEU", "application/java-archive")})
        assert r.status_code == 409
        r = client.post(f"{base}/upload",
                        data={"path": "uploads/box.jar", "overwrite": "true"},
                        files={"file": ("box.jar", b"NEU", "application/java-archive")})
        assert r.status_code == 200
        assert (basis / "uploads" / "box.jar").read_bytes() == b"NEU"

    def test_geschuetzte_ziele_ueber_api(self, client, instanz, basis):
        base = f"/api/instances/{instanz['id']}/files"
        (basis / "server.properties").write_text("x", encoding="utf-8")
        r = client.get(f"{base}/content", params={"path": "server.properties"})
        assert r.status_code == 400
        r = client.delete(f"{base}", params={"path": "server.properties"})
        assert r.status_code == 400
        r = client.get(f"{base}", params={"path": ""})
        names = [e["name"] for e in r.json()["entries"]]
        assert "server.properties" in names

    def test_viewer_schreibgeschuetzt(self, client, instanz, admin_client_factory=None):
        # Viewer-Cookie setzen (Setup + Viewer-Login)
        client.post("/api/auth/setup", json={"username": "boss",
                                             "password": "passwort123"})
        client.post("/api/auth/users", json={"username": "leser",
                                             "password": "passwort123",
                                             "role": "viewer"})
        client.cookies.clear()
        client.post("/api/auth/login", json={"username": "leser",
                                             "password": "passwort123"})
        base = f"/api/instances/{instanz['id']}/files"
        assert client.get(f"{base}", params={"path": ""}).status_code == 200
        assert client.post(f"{base}/mkdir",
                           json={"path": "x"}).status_code == 403
        assert client.put(f"{base}/content",
                          json={"path": "x.txt", "content": "y"}).status_code == 403
        assert client.delete(f"{base}",
                             params={"path": "eula.txt"}).status_code == 403
