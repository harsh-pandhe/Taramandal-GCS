"""Signature gate on POST /api/upload-trajectory (issue #49).

Acceptance criteria under test:
  * a validly signed trajectory uploads and the response names the signer;
  * a trajectory with one modified byte is refused with 403;
  * an unsigned trajectory is refused by default and only accepted when the
    GCS runs with --allow-unsigned (GCS_ALLOW_UNSIGNED=1).

The in-process tests drive the real FastAPI app through TestClient with the
module-level trust policy and DroneManager swapped out (same pattern as
test_status_endpoint.py). Because the allow-unsigned switch is decided when
the GCS process starts, the last test also spawns the real uvicorn process
with and without it, the way `tara up` launches it.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient

from tara_core.signing import TrustedKey, generate_private_key, public_key_to_b64, sign_show
from tara_gcs import main as gcs_main
from tara_gcs.auth import OperatorStore, Role
from tara_gcs.drone_manager import DroneManager
from tara_gcs.show_auth import ShowTrustPolicy, load_trust_policy

SHOW = json.dumps({"0": [
    {"time": 0.0, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0},
    {"time": 8.0, "x": 0.0, "y": 0.0, "z": -3.0, "yaw": 0.0},
]}).encode()


@pytest.fixture
def key():
    return generate_private_key()


@pytest.fixture
def gcs(monkeypatch, key):
    """A TestClient on the real app, trusting ``key`` as "Harsh", on an unconnected sim fleet.

    Not entered as a context manager, so the app's startup hook (which would
    connect the module's default PX4 fleet) never runs.
    """
    trusted = TrustedKey(signer="Harsh", public_key=key.public_key())
    policy = ShowTrustPolicy(trusted_keys={trusted.key_id: trusted})
    monkeypatch.setattr(gcs_main, "trust_policy", policy)
    monkeypatch.setattr(gcs_main, "drone_manager", DroneManager(ports=[0], backend="sim"))
    return TestClient(gcs_main.app), policy


def _upload(client, content, manifest=None, filename="show.json"):
    files = {"file": (filename, content, "application/json")}
    if manifest is not None:
        files["signature"] = (filename + ".sig.json", manifest, "application/json")
    return client.post("/api/upload-trajectory", files=files)


def test_signed_trajectory_uploads_and_reports_signer(gcs, key):
    client, _ = gcs

    resp = _upload(client, SHOW, sign_show(SHOW, "show.json", key))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["signature"]["verified"] is True
    assert body["signature"]["signer"] == "Harsh"
    assert gcs_main.drone_manager.active_trajectory is not None


def test_one_modified_byte_is_forbidden_and_not_loaded(gcs, key):
    client, _ = gcs
    manifest = sign_show(SHOW, "show.json", key)
    tampered = SHOW.replace(b"-3.0", b"-4.0")
    assert len(tampered) == len(SHOW)

    resp = _upload(client, tampered, manifest)

    assert resp.status_code == 403
    assert "modified after signing" in resp.json()["detail"]
    assert gcs_main.drone_manager.active_trajectory is None


def test_untrusted_signer_is_forbidden(gcs):
    client, _ = gcs
    rogue = generate_private_key()

    resp = _upload(client, SHOW, sign_show(SHOW, "show.json", rogue))

    assert resp.status_code == 403
    assert "not a trusted key" in resp.json()["detail"]


def test_unsigned_is_forbidden_by_default_with_operator_guidance(gcs):
    client, _ = gcs

    resp = _upload(client, SHOW)

    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert "unsigned" in detail and "--allow-unsigned" in detail and "tara sign" in detail
    assert gcs_main.drone_manager.active_trajectory is None


def test_allow_unsigned_accepts_legacy_file(gcs):
    client, policy = gcs
    policy.allow_unsigned = True

    resp = _upload(client, SHOW)

    assert resp.status_code == 200, resp.text
    assert resp.json()["signature"] == {"verified": False}


def test_allow_unsigned_still_rejects_a_bad_signature(gcs, key):
    """The flag admits files with no signature, not files whose signature fails."""
    client, policy = gcs
    policy.allow_unsigned = True
    manifest = sign_show(SHOW, "show.json", key)

    resp = _upload(client, SHOW + b" ", manifest)

    assert resp.status_code == 403


def test_signed_upload_with_no_trusted_keys_explains_the_config(gcs, key):
    client, policy = gcs
    policy.trusted_keys.clear()

    resp = _upload(client, SHOW, sign_show(SHOW, "show.json", key))

    assert resp.status_code == 403
    assert "trusted_keys in tara.toml" in resp.json()["detail"]


def test_signed_but_unsafe_trajectory_still_fails_validation(gcs, key):
    """A signature proves origin, not safety — the collision check still runs."""
    client, _ = gcs
    colliding = json.dumps({
        "0": [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0}, {"time": 5.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0}],
        "1": [{"time": 0.0, "x": 0.1, "y": 0.0, "z": -2.0, "yaw": 0.0}, {"time": 5.0, "x": 0.1, "y": 0.0, "z": -2.0, "yaw": 0.0}],
    }).encode()

    resp = _upload(client, colliding, sign_show(colliding, "show.json", key))

    assert resp.status_code == 400
    assert "safety check failed" in resp.json()["detail"]


def _write_toml(path, entries):
    lines = ["[security]", "trusted_keys = ["]
    lines += [f'    {{ signer = "{s}", public_key = "{k}" }},' for s, k in entries]
    lines.append("]")
    path.write_text("\n".join(lines) + "\n")


def test_load_trust_policy_reads_keys_and_skips_bad_entries(tmp_path, key):
    config = tmp_path / "tara.toml"
    _write_toml(config, [("Harsh", public_key_to_b64(key.public_key())), ("Typo", "AAAA")])

    policy = load_trust_policy(config)

    assert [k.signer for k in policy.trusted_keys.values()] == ["Harsh"]
    assert policy.allow_unsigned is False


def test_load_trust_policy_without_a_config_trusts_nothing(tmp_path):
    policy = load_trust_policy(tmp_path / "missing.toml")
    assert policy.trusted_keys == {}


def test_repo_tara_toml_parses_and_trusts_nobody_by_default():
    assert load_trust_policy(gcs_main.REPO_ROOT / "tara.toml").trusted_keys == {}


# --- Real process: the allow-unsigned switch is fixed at GCS launch ----------

GCS_STARTUP_TIMEOUT_S = 20.0
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_gcs(port, config_path, allow_unsigned):
    env = dict(os.environ)
    env.update({
        "VEHICLE_BACKEND": "sim",
        "NUM_DRONES": "1",
        "GCS_PORT": str(port),
        "GCS_CONFIG_PATH": str(config_path),
        "GCS_STATE_PATH": str(config_path.parent / f"state-{port}.json"),
        "GCS_ALLOW_UNSIGNED": "1" if allow_unsigned else "0",
        # Issue #64: a throwaway operators file and audit log, not the developer's.
        "GCS_OPERATORS_PATH": str(config_path.parent / "operators.json"),
        "GCS_COMMAND_AUDIT_PATH": str(config_path.parent / f"command-audit-{port}.jsonl"),
        "PYTHONPATH": os.pathsep.join(
            os.path.join(REPO_ROOT, "packages", p) for p in ("tara-core", "tara-gcs", "tara-sim")
        ),
    })
    # Same invocation `tara up` uses (apps/tara/tara_cli/services.py).
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tara_gcs.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=os.path.join(REPO_ROOT, "packages", "tara-gcs"),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for(base_url):
    deadline = time.time() + GCS_STARTUP_TIMEOUT_S
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{base_url}/api/status", timeout=1.0)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"GCS at {base_url} did not come up")


def _http_login(base_url, operator, passphrase):
    req = urllib.request.Request(
        f"{base_url}/api/auth/login", method="POST",
        data=json.dumps({"operator": operator, "passphrase": passphrase}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return json.loads(resp.read())["token"]


def _http_upload(base_url, content, manifest=None, token=None):
    boundary = "----tara49"
    parts = [("file", "show.json", content)]
    if manifest is not None:
        parts.append(("signature", "show.json.sig.json", manifest))
    body = b""
    for name, filename, data in parts:
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: application/json\r\n\r\n"
        ).encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{base_url}/api/upload-trajectory", data=body, method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.mark.parametrize("allow_unsigned", [False, True])
def test_gcs_process_enforces_signatures_per_launch_flag(tmp_path, key, allow_unsigned):
    config = tmp_path / "tara.toml"
    _write_toml(config, [("Harsh", public_key_to_b64(key.public_key()))])
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    OperatorStore(tmp_path / "operators.json").add("uploader", Role.CHOREOGRAPHER, "uploader passphrase")
    proc = _spawn_gcs(port, config, allow_unsigned)
    try:
        _wait_for(base_url)

        # Issue #64: the real server refuses an upload from nobody.
        status, _ = _http_upload(base_url, SHOW, sign_show(SHOW, "show.json", key))
        assert status == 401
        token = _http_login(base_url, "uploader", "uploader passphrase")

        status, body = _http_upload(base_url, SHOW, sign_show(SHOW, "show.json", key), token=token)
        assert status == 200 and body["signature"]["signer"] == "Harsh"

        status, _ = _http_upload(
            base_url, SHOW.replace(b"-3.0", b"-4.0"), sign_show(SHOW, "show.json", key), token=token,
        )
        assert status == 403

        status, body = _http_upload(base_url, SHOW, token=token)
        if allow_unsigned:
            assert status == 200 and body["signature"] == {"verified": False}
        else:
            assert status == 403 and "--allow-unsigned" in body["detail"]
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_show_signed_by_studio_export_uploads(gcs, key, tmp_path, monkeypatch):
    """Issue #49 end to end: the Studio's own /api/export signs, this GCS accepts.

    The Studio holds the private key the GCS trusts as "Harsh"; the show and its
    manifest go from one real app to the other byte for byte, as the Studio's
    Transmit button sends them.
    """
    from tara_core.signing import private_key_to_pem
    from tara_studio import signing as studio_signing
    from tara_studio.server import app as studio_app

    key_path = tmp_path / "studio.pem"
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(private_key_to_pem(key))
    monkeypatch.setattr(studio_signing, "DEFAULT_TOML", tmp_path / "tara.toml")
    monkeypatch.setenv(studio_signing.SIGNING_KEY_ENV, str(key_path))
    from tara_core.geometry import quintic_segment

    def _certified_choreo():
        p0, p1 = (0.0, 0.0, -2.0), (5.0, 0.0, -2.0)
        half = 6.0
        dt = 1.0 / 20.0
        waypoints = []
        t = 0.0
        while t <= 2 * half + 1e-9:
            state = quintic_segment(p0, p1, half, t) if t <= half else quintic_segment(p1, p0, half, t - half)
            x, y, z = state["position"]
            waypoints.append({"time": round(t, 4), "x": x, "y": y, "z": z, "yaw": 0.0})
            t += dt
        return {"drones": [{"id": 0, "waypoints": waypoints}]}

    exported = TestClient(studio_app).post(
        "/api/export", json={"choreo": _certified_choreo(), "format": "json", "sign": True},
    ).json()
    client, _ = gcs
    resp = _upload(
        client, exported["content"].encode("utf-8"), exported["signature"]["content"].encode("utf-8"),
        filename=exported["filename"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["signature"]["signer"] == "Harsh"

    # And a Studio export signed by a key this GCS does not trust is refused.
    other = tmp_path / "other.pem"
    fd = os.open(other, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(private_key_to_pem(generate_private_key()))
    monkeypatch.setenv(studio_signing.SIGNING_KEY_ENV, str(other))
    exported = TestClient(studio_app).post(
        "/api/export", json={"choreo": _certified_choreo(), "format": "json", "sign": True},
    ).json()
    resp = _upload(client, exported["content"].encode("utf-8"), exported["signature"]["content"].encode("utf-8"),
                   filename=exported["filename"])
    assert resp.status_code == 403


def test_a_preview_only_studio_export_is_rejected_by_the_gcs(gcs, key, tmp_path, monkeypatch):
    """Issue #143 review (Vishal): preview_only exports can never be signed
    (enforced in tara_studio's /api/export), so the only thing stopping one
    from being uploaded and flown is this GCS's own default signed-required
    policy. Verified manually by running gate_show directly; this is the
    automated version, end to end through both real apps' HTTP layers, under
    the GCS's actual default launch policy (no --allow-unsigned).
    """
    from tara_core.signing import private_key_to_pem
    from tara_studio import signing as studio_signing
    from tara_studio.server import app as studio_app

    key_path = tmp_path / "studio.pem"
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(private_key_to_pem(key))
    monkeypatch.setattr(studio_signing, "DEFAULT_TOML", tmp_path / "tara.toml")
    monkeypatch.setenv(studio_signing.SIGNING_KEY_ENV, str(key_path))
    from tara_core.geometry import quintic_segment

    def _certified_choreo():
        p0, p1 = (0.0, 0.0, -2.0), (5.0, 0.0, -2.0)
        half = 6.0
        dt = 1.0 / 20.0
        waypoints = []
        t = 0.0
        while t <= 2 * half + 1e-9:
            state = quintic_segment(p0, p1, half, t) if t <= half else quintic_segment(p1, p0, half, t - half)
            x, y, z = state["position"]
            waypoints.append({"time": round(t, 4), "x": x, "y": y, "z": z, "yaw": 0.0})
            t += dt
        return {"drones": [{"id": 0, "waypoints": waypoints}]}

    # Studio's own export gate refuses to sign a preview export, so this is
    # the real, only shape a preview export's HTTP response can take.
    exported = TestClient(studio_app).post(
        "/api/export", json={"choreo": _certified_choreo(), "format": "json", "preview_only": True},
    ).json()
    assert exported.get("signature") is None

    client, _ = gcs
    resp = _upload(client, exported["content"].encode("utf-8"), filename=exported["filename"])

    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert "unsigned" in detail and "--allow-unsigned" in detail
