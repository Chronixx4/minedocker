"""Tests für das Server-Icon (app/icon.py + Routen)."""
import shutil
import struct

import pytest
from fastapi import HTTPException

from app import icon as icon_mod
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
    return instances.create_instance("Icon-Srv", "fabric", "1.21.4",
                                     accept_eula=True)


def png_64() -> bytes:
    """Minimales, strukturell korrektes 64x64-PNG (IHDR + leeres IDAT-IEND,
    ohne echte Bilddaten — die Validierung liest nur den Header)."""
    ihdr = struct.pack(">IIBBBBB", 64, 64, 8, 6, 0, 0, 0)  # 8-bit RGBA
    ihdr_chunk = struct.pack(">I", 13) + b"IHDR" + ihdr
    ihdr_crc = struct.pack(">I", 0)
    return (b"\x89PNG\r\n\x1a\n" + ihdr_chunk + ihdr_crc
            + struct.pack(">I", 0) + b"IEND" + struct.pack(">I", 0))


class TestValidatePng:
    def test_valides_64x64(self):
        icon_mod.validate_png_64(png_64())

    @pytest.mark.parametrize("breite, hoehe", ((63, 64), (64, 63), (128, 128)))
    def test_falsche_groesse_400(self, breite, hoehe):
        content = (b"\x89PNG\r\n\x1a\n"
                   + struct.pack(">I", 13) + b"IHDR"
                   + struct.pack(">IIBBBBB", breite, hoehe, 8, 6, 0, 0, 0))
        with pytest.raises(HTTPException) as e:
            icon_mod.validate_png_64(content)
        assert e.value.status_code == 400
        assert "64x64" in e.value.detail

    def test_kein_png_400(self):
        with pytest.raises(HTTPException) as e:
            icon_mod.validate_png_64(b"GIF89a" + b"\x00" * 24)
        assert e.value.status_code == 400

    def test_zu_kurz_400(self):
        with pytest.raises(HTTPException) as e:
            icon_mod.validate_png_64(b"\x89PNG")
        assert e.value.status_code == 400

    def test_ihdr_defekt_400(self):
        with pytest.raises(HTTPException) as e:
            icon_mod.validate_png_64(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        assert e.value.status_code == 400


class TestWriteDelete:
    def test_write_und_info(self, instanz):
        info = icon_mod.write_icon(instanz["id"], png_64())
        assert info["exists"] is True
        assert info["size_bytes"] == len(png_64())
        assert icon_mod.icon_path(instanz["id"]).name == "server-icon.png"
        # Kein .tmp-Rest
        assert not (instances.instance_dir(instanz["id"])
                    / "server-icon.png.tmp").exists()

    def test_ungueltiges_icon_wird_nicht_geschrieben(self, instanz):
        with pytest.raises(HTTPException):
            icon_mod.write_icon(instanz["id"], b"kein-png")
        assert not icon_mod.icon_exists(instanz["id"])

    def test_delete_und_404(self, instanz):
        icon_mod.write_icon(instanz["id"], png_64())
        assert icon_mod.delete_icon(instanz["id"]) == {"deleted": "server-icon.png"}
        with pytest.raises(HTTPException) as e:
            icon_mod.delete_icon(instanz["id"])
        assert e.value.status_code == 404

    def test_ersetzen_ueberschreibt(self, instanz):
        icon_mod.write_icon(instanz["id"], png_64())
        icon_mod.write_icon(instanz["id"], png_64())
        assert icon_mod.icon_exists(instanz["id"])

    def test_unbekannte_instanz_404(self):
        with pytest.raises(HTTPException) as e:
            icon_mod.write_icon("gibts-nicht", png_64())
        assert e.value.status_code == 404
        with pytest.raises(HTTPException) as e:
            icon_mod.delete_icon("gibts-nicht")
        assert e.value.status_code == 404


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------

class TestIconRouten:
    def test_get_404_dann_put_get_delete(self, client, instanz):
        assert client.get(f"/api/instances/{instanz['id']}/icon").status_code == 404
        r = client.put(f"/api/instances/{instanz['id']}/icon",
                       files={"file": ("icon.png", png_64(), "image/png")})
        assert r.status_code == 200 and r.json()["exists"] is True
        r = client.get(f"/api/instances/{instanz['id']}/icon")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/png")
        assert r.content == png_64()
        assert client.delete(f"/api/instances/{instanz['id']}/icon").status_code == 200
        assert client.get(f"/api/instances/{instanz['id']}/icon").status_code == 404

    def test_put_ungueltige_groesse_400(self, client, instanz):
        klein = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
                 + struct.pack(">IIBBBBB", 32, 32, 8, 6, 0, 0, 0))
        r = client.put(f"/api/instances/{instanz['id']}/icon",
                       files={"file": ("icon.png", klein, "image/png")})
        assert r.status_code == 400
        assert "64x64" in r.json()["detail"]

    def test_put_kein_png_400(self, client, instanz):
        r = client.put(f"/api/instances/{instanz['id']}/icon",
                       files={"file": ("x.png", b"kein-png-bytes-padding!!",
                                       "image/png")})
        assert r.status_code == 400

    def test_delete_ohne_icon_404(self, client, instanz):
        assert client.delete(f"/api/instances/{instanz['id']}/icon").status_code == 404

    def test_unbekannte_instanz_404(self, client):
        assert client.get("/api/instances/xxx/icon").status_code == 404
        assert client.put("/api/instances/xxx/icon",
                          files={"file": ("i.png", png_64(), "image/png")}
                          ).status_code == 404

    def test_viewer_schreibend_403(self, client, instanz):
        client.post("/api/auth/setup", json={"username": "boss",
                                             "password": "passwort123"})
        client.post("/api/auth/users", json={"username": "waechter",
                                             "password": "passwort123",
                                             "role": "viewer"})
        client.cookies.clear()
        client.post("/api/auth/login", json={"username": "waechter",
                                             "password": "passwort123"})
        # GET bleibt für Viewer offen
        assert client.get(f"/api/instances/{instanz['id']}/icon").status_code == 404
        assert client.put(f"/api/instances/{instanz['id']}/icon",
                          files={"file": ("i.png", png_64(), "image/png")}
                          ).status_code == 403
        assert client.delete(f"/api/instances/{instanz['id']}/icon").status_code == 403
