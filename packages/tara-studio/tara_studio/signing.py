"""Studio-side show signing (issue #49).

The GCS refuses a trajectory unless its detached ``.sig.json`` manifest
verifies against a key in its ``[security].trusted_keys`` (see
``tara_core.signing``). This module lets the Studio backend produce that
manifest for its own exports, so a show can go from Studio to the GCS
without a separate ``tara sign`` step.

The private key stays on this machine, in this process: the browser only ever
sees the key id and public key (issue #64: private keys are never served).
Its location comes from ``STUDIO_SIGNING_KEY`` or ``[studio].signing_key`` in
tara.toml, and it is read for each export rather than cached, so a key made
with ``tara keygen`` after the Studio started is picked up, and a key file
other users can read is refused every time rather than only at startup.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tara_core.signing import (
    MANIFEST_SUFFIX,
    SignatureError,
    file_format,
    key_fingerprint,
    private_key_from_pem,
    public_key_to_b64,
    sign_show,
)

SIGNING_KEY_ENV = "STUDIO_SIGNING_KEY"

#: Export formats the GCS accepts as a trajectory upload, so the only ones
#: worth signing. (.tara is a compiled on-board format; KML is for Google Earth.)
SIGNABLE_FORMATS = frozenset({"json", "csv"})

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TOML = REPO_ROOT / "tara.toml"

SETUP_HINT = (
    "Create one with `tara keygen ~/.config/taramandal/studio-signing.pem --signer \"<who>\"`, "
    "set [studio].signing_key to that path in tara.toml (or STUDIO_SIGNING_KEY), "
    "and add the printed line to [security].trusted_keys on the GCS."
)


class SigningUnavailable(Exception):
    """A signature was needed but no usable signing key is configured."""


@dataclass(frozen=True)
class StudioSigner:
    path: Path
    private_key: Ed25519PrivateKey

    @property
    def key_id(self) -> str:
        return key_fingerprint(self.private_key.public_key())

    @property
    def public_key_b64(self) -> str:
        return public_key_to_b64(self.private_key.public_key())


def signing_key_path(toml_path: Path | None = None, environ: Mapping[str, str] = os.environ) -> Path | None:
    toml_path = DEFAULT_TOML if toml_path is None else toml_path
    raw = environ.get(SIGNING_KEY_ENV)
    if not raw and toml_path is not None and Path(toml_path).exists():
        raw = tomllib.loads(Path(toml_path).read_text()).get("studio", {}).get("signing_key")
    return Path(raw).expanduser() if raw else None


def load_signer(toml_path: Path | None = None, environ: Mapping[str, str] = os.environ) -> StudioSigner | None:
    """The configured signer, or None if no key is configured.

    Raises:
        SigningUnavailable: a key is configured but missing, readable by
            other users, or not an Ed25519 private key.
    """
    path = signing_key_path(toml_path, environ)
    if path is None:
        return None
    if not path.is_file():
        raise SigningUnavailable(f"signing key {path} does not exist. {SETUP_HINT}")
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise SigningUnavailable(
                f"signing key {path} has permissions {mode:o}, so other users can read it. "
                f"Run `chmod 600 {path}`."
            )
    try:
        return StudioSigner(path=path, private_key=private_key_from_pem(path.read_bytes()))
    except SignatureError as exc:
        raise SigningUnavailable(f"{path}: {exc}") from exc


def signing_status(toml_path: Path | None = None, environ: Mapping[str, str] = os.environ) -> dict:
    """What the Transmit panel shows. Public material only, never the private key."""
    try:
        signer = load_signer(toml_path, environ)
    except SigningUnavailable as exc:
        return {"configured": True, "ready": False, "error": str(exc)}
    if signer is None:
        return {"configured": False, "ready": False, "error": f"No signing key configured. {SETUP_HINT}"}
    return {
        "configured": True,
        "ready": True,
        "key_id": signer.key_id,
        "public_key": signer.public_key_b64,
    }


def sign_export(
    content: bytes,
    filename: str,
    sign: bool | None,
    toml_path: Path | None = None,
    environ: Mapping[str, str] = os.environ,
) -> dict | None:
    """The detached manifest for an export, or None if it is not signed.

    ``sign`` is a tri-state: True requires a signature (SigningUnavailable if
    that is impossible), False never signs, None signs whenever a key is
    configured and the format is one the GCS uploads.
    """
    if sign is False:
        return None
    fmt = file_format(filename)
    if fmt not in SIGNABLE_FORMATS:
        if sign:
            raise SigningUnavailable(f".{fmt} exports are not GCS uploads and are not signed")
        return None
    signer = load_signer(toml_path, environ)
    if signer is None:
        if sign:
            raise SigningUnavailable(f"No signing key configured. {SETUP_HINT}")
        return None
    manifest = sign_show(content, filename, signer.private_key)
    return {
        "filename": filename + MANIFEST_SUFFIX,
        "content": manifest.decode("utf-8"),
        "key_id": signer.key_id,
    }
