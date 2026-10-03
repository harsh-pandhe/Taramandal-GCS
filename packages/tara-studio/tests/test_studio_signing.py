"""Issue #49, Studio half: exports carry a .sig.json manifest the GCS accepts.

Verified with tara_core.signing.verify_show, the same function the GCS's
/api/upload-trajectory gate calls, against a trusted key built only from
what the Studio exposes publicly (its key id and public key).
"""

import os
import stat

import pytest
from fastapi.testclient import TestClient

from tara_core.signing import (
    SignatureError,
    TrustedKey,
    generate_private_key,
    private_key_to_pem,
    public_key_from_b64,
    verify_show,
)
from tara_studio import signing
from tara_studio.generator import generate_circle
from tara_studio.server import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def no_ambient_config(tmp_path, monkeypatch):
    """Neither the developer's tara.toml nor their environment leaks in."""
    monkeypatch.delenv(signing.SIGNING_KEY_ENV, raising=False)
    monkeypatch.setattr(signing, "DEFAULT_TOML", tmp_path / "tara.toml")


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    path = tmp_path / "studio-signing.pem"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(private_key_to_pem(generate_private_key()))
    monkeypatch.setenv(signing.SIGNING_KEY_ENV, str(path))
    return path


@pytest.fixture(scope="module")
def choreo():
    return generate_circle(3, 12.0, 2.0)


def export(choreo, fmt="json", **extra):
    return client.post("/api/export", json={"choreo": choreo, "format": fmt, **extra})


def trusted_from_status():
    status = client.get("/api/signing").json()
    key = TrustedKey(signer="Studio under test", public_key=public_key_from_b64(status["public_key"]))
    assert key.key_id == status["key_id"]
    return {key.key_id: key}


class TestNotConfigured:
    def test_status_explains_the_setup(self):
        status = client.get("/api/signing").json()
        assert status["configured"] is False and status["ready"] is False
        assert "tara keygen" in status["error"]

    def test_auto_exports_unsigned(self, choreo):
        res = export(choreo)
        assert res.status_code == 200
        assert "signature" not in res.json()

    def test_a_required_signature_is_a_409_with_the_fix(self, choreo):
        res = export(choreo, sign=True)
        assert res.status_code == 409
        assert "tara keygen" in res.json()["detail"]

    def test_key_from_tara_toml(self, tmp_path, key_file, monkeypatch):
        monkeypatch.delenv(signing.SIGNING_KEY_ENV)
        (tmp_path / "tara.toml").write_text(f'[studio]\nsigning_key = "{key_file}"\n')
        assert client.get("/api/signing").json()["ready"] is True


class TestSigned:
    @pytest.mark.parametrize("fmt", ["json", "csv"])
    def test_export_verifies_with_the_gcs_check(self, key_file, choreo, fmt):
        body = export(choreo, fmt).json()
        sig = body["signature"]
        assert sig["filename"] == body["filename"] + ".sig.json"
        verified = verify_show(
            body["content"].encode("utf-8"), body["filename"], sig["content"].encode("utf-8"), trusted_from_status(),
        )
        assert verified.signer == "Studio under test"
        assert verified.key_id == sig["key_id"]

    def test_one_changed_byte_fails_verification(self, key_file, choreo):
        body = export(choreo).json()
        tampered = body["content"].replace("0", "1", 1).encode("utf-8")
        with pytest.raises(SignatureError):
            verify_show(tampered, body["filename"], body["signature"]["content"].encode(), trusted_from_status())

    def test_sign_false_opts_out(self, key_file, choreo):
        assert "signature" not in export(choreo, sign=False).json()

    @pytest.mark.parametrize("fmt", ["kml", "tara"])
    def test_non_upload_formats_are_not_signed(self, key_file, choreo, fmt):
        assert "signature" not in export(choreo, fmt).json()
        assert export(choreo, fmt, sign=True).status_code == 400

    def test_status_never_serves_the_private_key(self, key_file):
        res = client.get("/api/signing")
        status = res.json()
        assert status["ready"] is True and len(status["key_id"]) == 16
        assert "PRIVATE KEY" not in res.text
        pem_body = "".join(key_file.read_text().splitlines()[1:-1])
        assert pem_body not in res.text

    def test_a_key_made_after_startup_is_picked_up(self, tmp_path, monkeypatch, choreo):
        path = tmp_path / "later.pem"
        monkeypatch.setenv(signing.SIGNING_KEY_ENV, str(path))
        assert export(choreo, sign=True).status_code == 409
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(private_key_to_pem(generate_private_key()))
        assert "signature" in export(choreo).json()


class TestUnusableKey:
    @pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
    def test_a_key_others_can_read_is_refused_not_silently_skipped(self, key_file, choreo):
        os.chmod(key_file, 0o644)
        assert "chmod 600" in client.get("/api/signing").json()["error"]
        res = export(choreo)  # auto: a configured but unusable key is an error
        assert res.status_code == 409
        assert "chmod 600" in res.json()["detail"]
        os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)
        assert export(choreo).status_code == 200

    def test_missing_and_garbage_keys(self, tmp_path, monkeypatch, choreo):
        monkeypatch.setenv(signing.SIGNING_KEY_ENV, str(tmp_path / "nope.pem"))
        assert "does not exist" in client.get("/api/signing").json()["error"]
        bogus = tmp_path / "bogus.pem"
        bogus.write_text("not a key")
        bogus.chmod(0o600)
        monkeypatch.setenv(signing.SIGNING_KEY_ENV, str(bogus))
        assert client.get("/api/signing").json()["ready"] is False
        assert export(choreo).status_code == 409
