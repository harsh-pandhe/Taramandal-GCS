"""Which show files the GCS will accept (issue #49).

Every trajectory upload must come with a detached Ed25519 signature manifest
(see :mod:`tara_core.signing`) from a key listed under ``[security]`` in
tara.toml. Unsigned files are refused unless the GCS was started with
``--allow-unsigned`` (``GCS_ALLOW_UNSIGNED=1``), which exists for legacy files
and local development, and is deliberately a per-launch flag rather than a
tara.toml setting: tara.toml is committed, and switching off authentication
for everyone who pulls should not be a one-line config change.

A bad trusted-key entry is logged and skipped rather than raised. Refusing to
start would also take away RTL and land from an operator who has drones in
the air, over a typo in a key they may not even be using.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from tara_core.signing import (
    SignatureError,
    TrustedKey,
    VerifiedSignature,
    public_key_from_b64,
    verify_show,
)

logger = logging.getLogger("TaramandalGCS")


@dataclass
class ShowTrustPolicy:
    trusted_keys: dict[str, TrustedKey] = field(default_factory=dict)
    allow_unsigned: bool = False

    def check(self, content: bytes, filename: str, manifest: bytes | None) -> VerifiedSignature | None:
        """Return who signed ``content``, or None for an unsigned file this policy allows.

        Raises:
            SignatureError: The file is unsigned and unsigned files are not
                allowed, or its signature does not verify. A manifest that is
                present but bad is rejected even under ``allow_unsigned`` —
                that flag admits legacy files, not broken signatures.
        """
        if manifest is None:
            if self.allow_unsigned:
                logger.warning(f"Accepting UNSIGNED trajectory {filename!r} (--allow-unsigned is set).")
                return None
            raise SignatureError(
                "trajectory is unsigned. Upload it together with its .sig.json signature "
                "manifest (create one with `tara sign`), or restart the GCS with "
                "--allow-unsigned to accept legacy files."
            )
        if not self.trusted_keys:
            raise SignatureError(
                "this GCS has no trusted signing keys — add the signer's public key to "
                "[security].trusted_keys in tara.toml and restart the GCS."
            )
        return verify_show(content, filename, manifest, self.trusted_keys)


def load_trust_policy(config_path: Path, *, allow_unsigned: bool = False) -> ShowTrustPolicy:
    """Read ``[security].trusted_keys`` from tara.toml at ``config_path``."""
    policy = ShowTrustPolicy(allow_unsigned=allow_unsigned)
    try:
        data = tomllib.loads(config_path.read_text())
    except FileNotFoundError:
        logger.warning(f"{config_path} not found — no trusted show-signing keys loaded.")
        return policy
    except tomllib.TOMLDecodeError as exc:
        logger.error(f"{config_path} is not valid TOML ({exc}) — no trusted show-signing keys loaded.")
        return policy

    entries = data.get("security", {}).get("trusted_keys", [])
    for i, entry in enumerate(entries):
        where = f"[security].trusted_keys[{i}] in {config_path}"
        if not isinstance(entry, dict) or not entry.get("signer") or not entry.get("public_key"):
            logger.error(f"Skipping {where}: needs both 'signer' and 'public_key'.")
            continue
        try:
            key = TrustedKey(signer=str(entry["signer"]), public_key=public_key_from_b64(str(entry["public_key"])))
        except SignatureError as exc:
            logger.error(f"Skipping {where} ({entry['signer']}): {exc}")
            continue
        policy.trusted_keys[key.key_id] = key

    logger.info(
        f"Show signing: {len(policy.trusted_keys)} trusted key(s)"
        + ("; UNSIGNED trajectories allowed." if allow_unsigned else "; unsigned trajectories rejected.")
    )
    return policy
