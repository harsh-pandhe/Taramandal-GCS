"""Ed25519 signatures for show trajectory files (issue #49).

A signed show is two files: the trajectory exactly as exported (``show.json``,
``show.csv`` or ``show.skyc``) and a detached manifest next to it
(``show.json.sig.json``). The manifest is detached rather than embedded
because a signature over "the file" has to cover its exact bytes, and neither
CSV nor JSON can carry a signature over their own bytes without a
canonicalization scheme every other consumer of those files would then trip
over. The trajectory stays readable by any tool; only the GCS needs the
manifest.

The manifest binds the SHA-256 of the file bytes, their length and the file's
format (extension), and names the signing key by its fingerprint. The Ed25519
signature covers the canonical JSON encoding of every other manifest field, so
changing a single byte of the trajectory — or of the manifest — fails
verification.

Who a key belongs to is deliberately *not* asserted by the manifest: the
verifier maps the fingerprint to a signer name from its own trusted-key list,
so a manifest can't claim to be from someone it isn't.

Like the rest of tara_core this module does no I/O; reading key files and
configuration is the caller's job. It is not re-exported from
``tara_core/__init__`` so that importing tara_core for geometry or validation
doesn't pull in ``cryptography``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

MANIFEST_SCHEMA = "tara-show-signature/1"
MANIFEST_SUFFIX = ".sig.json"

# Hard ceiling on a manifest's size. A real one is ~400 bytes; anything much
# bigger is not a manifest, and the verifier shouldn't JSON-parse it.
MAX_MANIFEST_BYTES = 16 * 1024


class SignatureError(ValueError):
    """A show file's signature is missing, malformed, untrusted or wrong."""


@dataclass(frozen=True)
class TrustedKey:
    """A public key the GCS accepts show signatures from."""

    signer: str
    public_key: Ed25519PublicKey

    @property
    def key_id(self) -> str:
        return key_fingerprint(self.public_key)


@dataclass(frozen=True)
class VerifiedSignature:
    """What a successful verification establishes about a show file."""

    key_id: str
    signer: str
    signed_at: str


def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def private_key_to_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def private_key_from_pem(data: bytes) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as exc:
        raise SignatureError(f"not a readable unencrypted PEM private key: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SignatureError("private key is not an Ed25519 key")
    return key


def public_key_to_b64(key: Ed25519PublicKey) -> str:
    """The 32 raw public-key bytes, base64 — the form tara.toml stores."""
    raw = key.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def public_key_from_b64(text: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SignatureError(f"public key is not valid base64: {exc}") from exc
    if len(raw) != 32:
        raise SignatureError(f"an Ed25519 public key is 32 bytes, got {len(raw)}")
    return Ed25519PublicKey.from_public_bytes(raw)


def key_fingerprint(key: Ed25519PublicKey) -> str:
    """Short stable id for a public key: first 16 hex chars of SHA-256 of its raw bytes."""
    raw = key.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def file_format(filename: str) -> str:
    """The format a file will be parsed as, i.e. its lower-cased extension without the dot."""
    return PurePath(filename).suffix.lower().lstrip(".")


def _signed_payload(manifest: Mapping[str, object]) -> bytes:
    unsigned = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def sign_show(
    content: bytes,
    filename: str,
    private_key: Ed25519PrivateKey,
    *,
    signed_at: datetime | None = None,
) -> bytes:
    """Build the detached signature manifest for ``content`` and return it as JSON bytes."""
    manifest: dict[str, object] = {
        "schema": MANIFEST_SCHEMA,
        "format": file_format(filename),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "key_id": key_fingerprint(private_key.public_key()),
        "signed_at": (signed_at or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
    }
    manifest["signature"] = base64.b64encode(private_key.sign(_signed_payload(manifest))).decode("ascii")
    return (json.dumps(manifest, indent=2) + "\n").encode("utf-8")


def verify_show(
    content: bytes,
    filename: str,
    manifest_bytes: bytes,
    trusted_keys: Mapping[str, TrustedKey],
) -> VerifiedSignature:
    """Check ``content`` against its detached manifest and the trusted keys.

    Args:
        content: The trajectory file's bytes, exactly as uploaded.
        filename: The uploaded file's name; its extension must match the
            format the manifest was signed for.
        manifest_bytes: The ``.sig.json`` manifest's bytes.
        trusted_keys: Trusted keys, keyed by :func:`key_fingerprint`.

    Raises:
        SignatureError: With an operator-readable reason, if anything about
            the signature does not check out.
    """
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise SignatureError("signature manifest is too large to be a manifest")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SignatureError(f"signature manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SignatureError("signature manifest must be a JSON object")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise SignatureError(f"unsupported signature manifest schema {manifest.get('schema')!r}")
    for field in ("format", "sha256", "size", "key_id", "signed_at", "signature"):
        if field not in manifest:
            raise SignatureError(f"signature manifest is missing '{field}'")

    key_id = manifest["key_id"]
    trusted = trusted_keys.get(key_id) if isinstance(key_id, str) else None
    if trusted is None:
        raise SignatureError(f"signed by key {key_id!r}, which is not a trusted key on this GCS")

    # Signature first: until it verifies, nothing else in the manifest is
    # known to come from the key holder, so its fields aren't worth reporting.
    try:
        signature = base64.b64decode(str(manifest["signature"]), validate=True)
        trusted.public_key.verify(signature, _signed_payload(manifest))
    except (binascii.Error, ValueError, InvalidSignature) as exc:
        raise SignatureError("signature manifest has been altered or was not signed by this key") from exc

    if manifest["format"] != file_format(filename):
        raise SignatureError(
            f"manifest signs a .{manifest['format']} file but the upload is .{file_format(filename)}"
        )
    if manifest["size"] != len(content) or manifest["sha256"] != hashlib.sha256(content).hexdigest():
        raise SignatureError("trajectory file does not match its signature — it was modified after signing")

    return VerifiedSignature(key_id=trusted.key_id, signer=trusted.signer, signed_at=str(manifest["signed_at"]))
