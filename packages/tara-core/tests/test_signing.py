"""Tests for tara_core.signing — detached Ed25519 manifests over show files (issue #49)."""

import json

import pytest

from tara_core.signing import (
    SignatureError,
    TrustedKey,
    generate_private_key,
    key_fingerprint,
    private_key_from_pem,
    private_key_to_pem,
    public_key_from_b64,
    public_key_to_b64,
    sign_show,
    verify_show,
)

SHOW = json.dumps({"0": [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0}]}).encode()


@pytest.fixture
def key():
    return generate_private_key()


@pytest.fixture
def trusted(key):
    t = TrustedKey(signer="Studio laptop", public_key=key.public_key())
    return {t.key_id: t}


def test_valid_signature_reports_the_configured_signer(key, trusted):
    manifest = sign_show(SHOW, "show.json", key)

    verified = verify_show(SHOW, "show.json", manifest, trusted)

    assert verified.signer == "Studio laptop"
    assert verified.key_id == key_fingerprint(key.public_key())


def test_every_single_byte_change_fails(key, trusted):
    manifest = sign_show(SHOW, "show.json", key)
    for i in range(len(SHOW)):
        tampered = bytearray(SHOW)
        tampered[i] ^= 0x01
        with pytest.raises(SignatureError, match="modified after signing"):
            verify_show(bytes(tampered), "show.json", manifest, trusted)


def test_appended_bytes_fail(key, trusted):
    manifest = sign_show(SHOW, "show.json", key)
    with pytest.raises(SignatureError):
        verify_show(SHOW + b"\n", "show.json", manifest, trusted)


def test_manifest_rewritten_to_match_tampered_file_fails(key, trusted):
    """Updating the hash in the manifest to cover altered bytes breaks the signature."""
    import hashlib

    tampered = SHOW.replace(b"-2.0", b"-9.0")
    manifest = json.loads(sign_show(SHOW, "show.json", key))
    manifest["sha256"] = hashlib.sha256(tampered).hexdigest()

    with pytest.raises(SignatureError, match="altered"):
        verify_show(tampered, "show.json", json.dumps(manifest).encode(), trusted)


def test_untrusted_key_fails(trusted):
    rogue = generate_private_key()
    manifest = sign_show(SHOW, "show.json", rogue)
    with pytest.raises(SignatureError, match="not a trusted key"):
        verify_show(SHOW, "show.json", manifest, trusted)


def test_manifest_claiming_a_trusted_key_id_but_signed_by_another_fails(key, trusted):
    rogue = generate_private_key()
    manifest = json.loads(sign_show(SHOW, "show.json", rogue))
    manifest["key_id"] = key_fingerprint(key.public_key())
    with pytest.raises(SignatureError, match="altered"):
        verify_show(SHOW, "show.json", json.dumps(manifest).encode(), trusted)


def test_format_is_bound(key, trusted):
    manifest = sign_show(SHOW, "show.json", key)
    with pytest.raises(SignatureError, match="upload is .csv"):
        verify_show(SHOW, "show.csv", manifest, trusted)


def test_renaming_within_a_format_is_allowed(key, trusted):
    manifest = sign_show(SHOW, "show.json", key)
    assert verify_show(SHOW, "final-show-v2.JSON", manifest, trusted).signer == "Studio laptop"


@pytest.mark.parametrize("manifest", [b"", b"not json", b"[]", b'{"schema": "other/1"}', b"x" * 20000])
def test_malformed_manifests_are_signature_errors(manifest, trusted):
    with pytest.raises(SignatureError):
        verify_show(SHOW, "show.json", manifest, trusted)


def test_missing_field_is_named(key, trusted):
    manifest = json.loads(sign_show(SHOW, "show.json", key))
    del manifest["size"]
    with pytest.raises(SignatureError, match="missing 'size'"):
        verify_show(SHOW, "show.json", json.dumps(manifest).encode(), trusted)


def test_key_serialization_round_trips(key):
    assert key_fingerprint(private_key_from_pem(private_key_to_pem(key)).public_key()) == key_fingerprint(key.public_key())
    assert key_fingerprint(public_key_from_b64(public_key_to_b64(key.public_key()))) == key_fingerprint(key.public_key())


@pytest.mark.parametrize("text", ["not base64!!", "AAAA"])
def test_bad_public_keys_are_rejected(text):
    with pytest.raises(SignatureError):
        public_key_from_b64(text)
