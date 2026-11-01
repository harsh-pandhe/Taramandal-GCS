"""Issue #172: End-to-end Studio-built show flown through GCS.

Proves the complete software pipeline:
1. Studio builds a small show (3 drones).
2. /api/validate certifies it with zero jerk violations (preview_only=False).
3. Export signed (Ed25519) and verified against GCS trusted_keys policy.
4. GCS stages the trajectory and verifies geofence bounds.
5. Execution='streamed' starts successfully from FORMATION_HOLD -> RUNNING.
"""

from __future__ import annotations

import json
import os
import subprocess
import pytest
from fastapi.testclient import TestClient

from tara_core.signing import TrustedKey, generate_private_key, private_key_to_pem
from tara_gcs import main as gcs_main
from tara_gcs.auth import AuthService, LoginThrottle, OperatorStore, Role, SessionStore
from tara_gcs.drone_manager import DroneManager
from tara_gcs.fleet_fsm import LifecycleState
from tara_gcs.show_auth import ShowTrustPolicy
from tara_studio import signing as studio_signing
from tara_studio.server import app as studio_app
from tara_studio.validator import certify_feasibility_report


@pytest.fixture
def studio_show_3_drones() -> dict:
    """Build a certified 3-drone sequenced show via Studio's sequencer engine."""
    script = """
import('./packages/tara-studio-web/src/engine/sequencer.js').then(({ buildSequencedShow, makeScene }) => {
  const scenes = [
    makeScene({ name: 'Circle', shapeType: 'parametric', shapePayload: 'circle', holdDurationSeconds: 6, transitionDurationSeconds: 4 }),
  ];
  const show = buildSequencedShow(scenes, 3, 10);
  process.stdout.write(JSON.stringify({ drones: show.drones }));
});
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def test_issue_172_studio_to_gcs_e2e_pipeline(studio_show_3_drones, tmp_path, monkeypatch):
    choreo = studio_show_3_drones
    assert len(choreo["drones"]) == 3

    # Step 1: Feasibility and jerk certification in Studio validator
    is_certified, report = certify_feasibility_report(choreo)
    assert is_certified, f"Feasibility failed: {report['summary']}"
    assert report["violations"]["jerk"] == []
    assert report["metrics"]["max_jerk_m_s3"] <= 20.0
    planned_duration = choreo["drones"][0]["waypoints"][-1]["time"]
    assert planned_duration > 0

    # Step 2: Export signed from Studio
    key = generate_private_key()
    key_path = tmp_path / "studio.pem"
    key_path.write_bytes(private_key_to_pem(key))
    os.chmod(key_path, 0o600)
    monkeypatch.setenv(studio_signing.SIGNING_KEY_ENV, str(key_path))

    studio_client = TestClient(studio_app)
    export_res = studio_client.post("/api/export", json={"choreo": choreo, "format": "json", "sign": True})
    assert export_res.status_code == 200, export_res.text
    exported = export_res.json()
    assert exported["preview_only"] is False, "Certified show must not be preview_only"
    assert "signature" in exported and exported["signature"] is not None

    # Step 3: Setup GCS trusting Studio's key with Flight Commander RBAC
    trusted = TrustedKey(signer="Studio", public_key=key.public_key())
    policy = ShowTrustPolicy(trusted_keys={trusted.key_id: trusted}, allow_unsigned=False)
    manager = DroneManager(ports=[0, 1, 2], backend="sim", state_path=str(tmp_path / "show_state.json"))
    store = OperatorStore(tmp_path / "operators.json")
    store.add("harsh", Role.FLIGHT_COMMANDER, "correct horse battery staple")
    service = AuthService(store, SessionStore(), LoginThrottle(max_failures=3, window_s=60))

    monkeypatch.setattr(gcs_main, "trust_policy", policy)
    monkeypatch.setattr(gcs_main, "drone_manager", manager)
    monkeypatch.setattr(gcs_main, "auth_service", service)

    gcs_client = TestClient(gcs_main.app)
    login_res = gcs_client.post("/api/auth/login", json={"operator": "harsh", "passphrase": "correct horse battery staple"})
    assert login_res.status_code == 200
    headers = {"Authorization": f"Bearer {login_res.json()['token']}"}

    # Step 4: Upload signed show to GCS
    show_bytes = exported["content"].encode("utf-8")
    sig_bytes = exported["signature"]["content"].encode("utf-8")
    files = {
        "file": (exported["filename"], show_bytes, "application/json"),
        "signature": (exported["filename"] + ".sig.json", sig_bytes, "application/json"),
    }
    up_res = gcs_client.post("/api/upload-trajectory", files=files, headers=headers)
    assert up_res.status_code == 200, up_res.text
    assert manager.active_trajectory is not None
    assert set(manager.active_trajectory.keys()) == {0, 1, 2}
    for drone_id in (0, 1, 2):
        wps = manager.active_trajectory[drone_id]
        assert len(wps) > 0
        assert wps[-1]["time"] == pytest.approx(planned_duration, abs=0.01)

    # Step 5: Verify onboard execution is refused before takeoff
    start_onboard = gcs_client.post("/api/start-trajectory", json={"mode": "PARALLEL", "execution": "onboard"}, headers=headers)
    assert start_onboard.status_code in (400, 409), "Onboard execution from pads must be refused"

    # Step 6: Start show in streamed execution mode from FORMATION_HOLD
    manager.fleet_fsm._state = LifecycleState.FORMATION_HOLD
    start_res = gcs_client.post("/api/start-trajectory", json={"mode": "PARALLEL", "execution": "streamed"}, headers=headers)
    assert start_res.status_code == 200, start_res.text
    assert manager.fleet_fsm.state == LifecycleState.RUNNING


def test_preview_only_export_refused_by_gcs_upload(studio_show_3_drones, tmp_path, monkeypatch):
    """Verify that uncertified or preview_only shows are strictly rejected by the GCS upload gate."""
    choreo = studio_show_3_drones
    studio_client = TestClient(studio_app)
    # Request preview_only export
    export_res = studio_client.post("/api/export", json={"choreo": choreo, "format": "json", "preview_only": True})
    assert export_res.status_code == 200
    preview_exported = export_res.json()
    assert preview_exported["preview_only"] is True

    # Setup GCS with default policy (unsigned disallowed)
    manager = DroneManager(ports=[0, 1, 2], backend="sim", state_path=str(tmp_path / "show_state.json"))
    store = OperatorStore(tmp_path / "operators.json")
    store.add("harsh", Role.FLIGHT_COMMANDER, "correct horse battery staple")
    service = AuthService(store, SessionStore(), LoginThrottle(max_failures=3, window_s=60))
    trusted = TrustedKey(signer="Studio", public_key=generate_private_key().public_key())
    policy = ShowTrustPolicy(trusted_keys={trusted.key_id: trusted}, allow_unsigned=False)

    monkeypatch.setattr(gcs_main, "trust_policy", policy)
    monkeypatch.setattr(gcs_main, "drone_manager", manager)
    monkeypatch.setattr(gcs_main, "auth_service", service)

    gcs_client = TestClient(gcs_main.app)
    login_res = gcs_client.post("/api/auth/login", json={"operator": "harsh", "passphrase": "correct horse battery staple"})
    headers = {"Authorization": f"Bearer {login_res.json()['token']}"}

    # Upload preview_only show (has no signature) -> must be rejected by GCS
    files = {"file": (preview_exported["filename"], preview_exported["content"].encode("utf-8"), "application/json")}
    up_res = gcs_client.post("/api/upload-trajectory", files=files, headers=headers)
    assert up_res.status_code == 403, "GCS must reject unsigned/preview_only shows when signatures are enforced"
    assert "--allow-unsigned" in up_res.text or "signature" in up_res.text.lower()

