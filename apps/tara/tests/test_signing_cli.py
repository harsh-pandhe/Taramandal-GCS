"""`tara keygen`, `tara sign` and `tara up --allow-unsigned` (issue #49)."""

import os
import re
import stat
from pathlib import Path

from typer.testing import CliRunner

from tara_cli.config import TaraConfig
from tara_cli.main import app
from tara_cli.services import build_services
from tara_core.signing import TrustedKey, public_key_from_b64, verify_show

runner = CliRunner()


def test_keygen_then_sign_produces_a_manifest_the_printed_key_verifies(tmp_path):
    key_path = tmp_path / "keys" / "studio.pem"
    result = runner.invoke(app, ["keygen", str(key_path), "--signer", "Studio laptop"])
    assert result.exit_code == 0, result.output
    assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600

    # The line keygen prints is what the operator pastes into tara.toml.
    match = re.search(r'\{ signer = "(.+)", public_key = "(.+)" \}', result.output)
    assert match, result.output
    trusted = TrustedKey(signer=match.group(1), public_key=public_key_from_b64(match.group(2)))

    show = tmp_path / "show.csv"
    show.write_text("drone_id,time,x,y,z,yaw\n0,0.0,0.0,0.0,-2.0,0.0\n")
    result = runner.invoke(app, ["sign", str(show), "--key", str(key_path)])
    assert result.exit_code == 0, result.output

    manifest = Path(str(show) + ".sig.json").read_bytes()
    verified = verify_show(show.read_bytes(), show.name, manifest, {trusted.key_id: trusted})
    assert verified.signer == "Studio laptop"


def test_keygen_refuses_to_overwrite_a_key(tmp_path):
    key_path = tmp_path / "studio.pem"
    key_path.write_text("existing")

    result = runner.invoke(app, ["keygen", str(key_path), "--signer", "x"])

    assert result.exit_code == 1
    assert key_path.read_text() == "existing"


def test_sign_rejects_a_non_key_file(tmp_path):
    show = tmp_path / "show.json"
    show.write_text("{}")
    bogus = tmp_path / "bogus.pem"
    bogus.write_text("not a key")

    result = runner.invoke(app, ["sign", str(show), "--key", str(bogus)])

    assert result.exit_code == 1
    assert not Path(str(show) + ".sig.json").exists()


def _gcs_env(**kwargs):
    config = TaraConfig(repo_root=Path("/repo"))
    services = build_services(config, num_drones=1, backend="sim", headless=True, rviz=False, **kwargs)
    return next(s for s in services if s.name == "gcs-api").env


def test_gcs_rejects_unsigned_unless_up_is_given_allow_unsigned():
    assert _gcs_env()["GCS_ALLOW_UNSIGNED"] == "0"
    assert _gcs_env(allow_unsigned=True)["GCS_ALLOW_UNSIGNED"] == "1"
