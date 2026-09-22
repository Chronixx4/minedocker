"""Tests für app.security: Dateinamen-Validierung, Pfadschutz, API-Key."""
import pytest
from fastapi import HTTPException

from app.security import safe_mods_path, validate_filename, validate_identifier


class TestValidateFilename:
    def test_gueltiger_name(self):
        assert validate_filename("fabric-api-1.0.jar") == "fabric-api-1.0.jar"

    def test_leer(self):
        with pytest.raises(HTTPException) as e:
            validate_filename("")
        assert e.value.status_code == 400

    def test_zu_lang(self):
        with pytest.raises(HTTPException):
            validate_filename("a" * 260 + ".jar")

    def test_slash_verboten(self):
        with pytest.raises(HTTPException):
            validate_filename("sub/dir.jar")

    def test_backslash_verboten(self):
        with pytest.raises(HTTPException):
            validate_filename("sub\\dir.jar")

    def test_doppelpunkt_verboten(self):
        with pytest.raises(HTTPException):
            validate_filename("a..b.jar")

    def test_nullbyte_verboten(self):
        with pytest.raises(HTTPException):
            validate_filename("a\x00.jar")

    def test_nur_jar_erlaubt(self):
        with pytest.raises(HTTPException):
            validate_filename("evil.txt")

    def test_muss_mit_buchstabe_ziffer_beginnen(self):
        with pytest.raises(HTTPException):
            validate_filename(".hidden.jar")


class TestValidateIdentifier:
    def test_gueltig(self):
        assert validate_identifier("A9-z._x", "Test") == "A9-z._x"

    def test_leer(self):
        with pytest.raises(HTTPException):
            validate_identifier("", "Test")

    def test_ungueltige_zeichen(self):
        with pytest.raises(HTTPException):
            validate_identifier("a b/c", "Test")

    def test_zu_lang(self):
        with pytest.raises(HTTPException):
            validate_identifier("a" * 65, "Test")


class TestSafeModsPath:
    def test_normale_datei(self, tmp_path):
        path = safe_mods_path(tmp_path, "mod.jar")
        assert path == (tmp_path / "mod.jar").resolve()

    def test_traversal_blockiert(self, tmp_path):
        with pytest.raises(HTTPException) as e:
            safe_mods_path(tmp_path, "../evil.jar")
        assert e.value.status_code == 400

    def test_absoluter_pfad_blockiert(self, tmp_path):
        with pytest.raises(HTTPException):
            safe_mods_path(tmp_path, "C:\\evil.jar")

    def test_symlink_escape_blockiert(self, tmp_path):
        try:
            # Ziel LIEGT AUSSERHALB des mods-Ordners (hängender Symlink):
            # resolve() folgt dem Link -> Pfad verlässt base -> Block.
            # (Ein Link auf ein Unterverzeichnis INNERHALB von base ist
            # kein Escape und wird zu Recht durchgelassen.)
            (tmp_path / "link.jar").symlink_to(tmp_path.parent / "escape-target.jar")
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks auf diesem System nicht verfügbar (Windows-Rechte)")
        with pytest.raises(HTTPException) as e:
            safe_mods_path(tmp_path, "link.jar")
        assert e.value.status_code == 400
