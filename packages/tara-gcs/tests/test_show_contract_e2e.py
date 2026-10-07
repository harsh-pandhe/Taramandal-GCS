"""Issue #55: one show contract from the Studio, through Tara Core, to the GCS.

These run the real apps end to end rather than the schema file on its own: a
show the Studio generates and signs is uploaded through the GCS's HTTP endpoint,
staged, compiled into the per-drone mission files the fleet flies, and decoded
back — asserting at every hop that nothing the Studio wrote (drone ids past 99,
LED channels, yaw, version) is lost or changed. The parity table pins the other
direction: anything the Studio refuses to export, the GCS refuses to stage.
"""

import asyncio
import io
import json
import logging
import math
import os
import zipfile

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tara_core import SHOW_FORMAT_VERSION, Show, compute_show_id, decode_mission
from tara_core.signing import TrustedKey, generate_private_key, private_key_to_pem
from tara_gcs import main as gcs_main
from tara_gcs.drone_manager import MISSION_FILE_NAME, MISSION_REMOTE_DIR, DroneManager, _pad_offset
from tara_gcs.show_auth import ShowTrustPolicy
from tara_gcs.state_store import load_show_state, save_show_state
from tara_gcs.trajectory_parser import gate_trajectory, parse_trajectory_bytes
from tara_studio import signing as studio_signing
from tara_studio.exporter import export_csv, export_json
from tara_studio.generator import generate_circle
from tara_studio.server import app as studio_app

REMOTE_PATH = f"{MISSION_REMOTE_DIR}/{MISSION_FILE_NAME}"

#: An LED track with a white channel, so the test can see where w goes.
LED_TRACK = [{"pattern": "SOLID", "start_time": 0, "duration": 9999, "color": "#FF800040"}]


@pytest.fixture
def key():
    return generate_private_key()


@pytest.fixture
def studio_key(tmp_path, monkeypatch, key):
    """The Studio signs with ``key``, which the GCS fixture trusts."""
    key_path = tmp_path / "studio.pem"
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(private_key_to_pem(key))
    monkeypatch.setattr(studio_signing, "DEFAULT_TOML", tmp_path / "tara.toml")
    monkeypatch.setenv(studio_signing.SIGNING_KEY_ENV, str(key_path))


@pytest.fixture
def gcs(monkeypatch, key, tmp_path):
    """The real GCS app trusting ``key``, unsigned uploads allowed, on an unconnected sim fleet."""
    trusted = TrustedKey(signer="Studio", public_key=key.public_key())
    policy = ShowTrustPolicy(trusted_keys={trusted.key_id: trusted}, allow_unsigned=True)
    manager = DroneManager(ports=[0], backend="sim", state_path=str(tmp_path / "show_state.json"))
    monkeypatch.setattr(gcs_main, "trust_policy", policy)
    monkeypatch.setattr(gcs_main, "drone_manager", manager)
    return TestClient(gcs_main.app)


def _studio_export(choreo: dict, **extra) -> dict:
    resp = TestClient(studio_app).post("/api/export", json={"choreo": choreo, "format": "json", **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _upload(client, content: bytes, manifest: bytes | None = None, filename: str = "show.json"):
    files = {"file": (filename, content, "application/octet-stream")}
    if manifest is not None:
        files["signature"] = (filename + ".sig.json", manifest, "application/json")
    return client.post("/api/upload-trajectory", files=files)


def _gcs_shape(show_file: dict) -> dict:
    """What the GCS must hold for a canonical show file: every drone, every key, time-ordered."""
    return {
        d["id"]: sorted(d["waypoints"], key=lambda w: w["time"])
        for d in show_file["drones"]
    }


def _wp(t: float, x: float, z: float, yaw: float = 0.0, **extra) -> dict:
    return {"time": t, "x": x, "y": 0.0, "z": z, "yaw": yaw, **extra}


def _two_drone_show(**wp_extra) -> dict:
    """Two drones 5 m apart, climbing; no collision, no version."""
    return {"drones": [
        {"id": i, "waypoints": [_wp(0.0, 5.0 * i, 0.0, **wp_extra), _wp(6.0, 5.0 * i, -3.0, yaw=90.0, **wp_extra)]}
        for i in (0, 1)
    ]}


# ------------------------------------------------------------- full pipeline


async def test_studio_show_of_120_drones_reaches_the_mission_files_intact(gcs, studio_key, tmp_path, monkeypatch):
    """Generate (120 drones) -> bake LED -> export + sign -> GCS upload ->
    active_trajectory -> upload_show -> decode_mission, with nothing lost."""
    # 0.5 Hz rather than the Studio UI's 1 Hz floor: at 1 Hz this show's
    # export is ~5.2 MB, past the GCS's 5 MB upload cap (see the PR notes).
    exported = _studio_export(generate_circle(120, 30.0, 0.5), sign=True, led_track=LED_TRACK)
    signed_bytes = exported["content"].encode("utf-8")
    show_file = json.loads(signed_bytes)
    assert show_file["version"] == SHOW_FORMAT_VERSION
    assert max(d["id"] for d in show_file["drones"]) == 119

    manager = DroneManager(ports=list(range(120)), backend="sim", state_path=str(tmp_path / "state.json"))
    monkeypatch.setattr(gcs_main, "drone_manager", manager)
    try:
        resp = _upload(gcs, signed_bytes, exported["signature"]["content"].encode("utf-8"),
                       filename=exported["filename"])
        assert resp.status_code == 200, resp.text
        assert resp.json()["signature"]["verified"] is True

        # Staged exactly as signed: ids >= 100, yaw, r/g/b/w all kept.
        staged = manager.active_trajectory
        assert staged == _gcs_shape(show_file)
        assert all(set(wp) >= {"r", "g", "b", "w"} for wps in staged.values() for wp in wps)

        await manager.connect_all()
        await _wait_until(lambda: all(
            t["connected"] and t["satellites"] >= 6 and t["battery_percent"] > 20
            for t in manager.telemetry.values()
        ), timeout=20.0)
        status = await manager.upload_show(staged)
        assert status["all_verified"], status["launch_gate"]
        assert status["verified_count"] == 120

        # Every healthy drone, in id order, takes the slot of the same index
        # (the remap's documented behaviour), so drone i flies show drone i.
        show_id = compute_show_id(staged)
        storage = manager._sim_fleet.state.storage
        for drone_id, wps in staged.items():
            header, flown = decode_mission(storage[drone_id][REMOTE_PATH])
            assert header.drone_id == drone_id and header.show_id == show_id
            assert header.has_led and len(flown) == len(wps)
            px, py = _pad_offset(drone_id)
            for src, got in zip(wps, flown):
                assert got["time"] == pytest.approx(src["time"], abs=5e-4)
                assert got["x"] == pytest.approx(src["x"] - px, abs=5e-3)
                assert got["y"] == pytest.approx(src["y"] - py, abs=5e-3)
                assert got["z"] == pytest.approx(src["z"], abs=5e-3)
                assert _yaw_delta(got["yaw"], src["yaw"]) <= 5e-3
                assert (got["r"], got["g"], got["b"]) == (src["r"], src["g"], src["b"])
                # The mission record has no white channel: w stops at the GCS
                # until the onboard format grows one (follow-up, #56).
                assert "w" not in got

        # The show-state snapshot taken mid-show round-trips to the same show.
        save_show_state(manager.state_path, staged, "LOCAL", 0.0, {})
        restored = load_show_state(manager.state_path)["trajectory"]
        assert gate_trajectory(restored) == staged
        assert compute_show_id(restored) == show_id
    finally:
        await manager.shutdown()


async def _wait_until(predicate, timeout: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.1)


def _yaw_delta(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def test_staged_show_is_exactly_the_signed_bytes(gcs, studio_key):
    """What the GCS stages is the signed file parsed by Core, not a re-reading of it."""
    exported = _studio_export(_two_drone_show(r=1, g=2, b=3, w=4, vx=0.5), sign=True)
    signed_bytes = exported["content"].encode("utf-8")
    resp = _upload(gcs, signed_bytes, exported["signature"]["content"].encode("utf-8"))
    assert resp.status_code == 200, resp.text

    expected = Show.validate_and_load(json.loads(signed_bytes)).to_gcs()
    assert gcs_main.drone_manager.active_trajectory == expected
    assert expected[1][1] == {"time": 6.0, "x": 5.0, "y": 0.0, "z": -3.0, "yaw": 90.0,
                              "r": 1, "g": 2, "b": 3, "w": 4, "vx": 0.5}
    # The response the dashboard renders carries the same waypoints.
    assert resp.json()["trajectory_data"]["1"] == expected[1]


# ------------------------------------------------------------ version policy


def test_newer_show_format_is_refused_with_a_clear_400(gcs):
    show = {"version": SHOW_FORMAT_VERSION + 1, **_two_drone_show()}
    resp = _upload(gcs, json.dumps(show).encode())
    assert resp.status_code == 400
    assert f"version {SHOW_FORMAT_VERSION + 1} is newer than this GCS understands" in resp.json()["detail"]
    assert gcs_main.drone_manager.active_trajectory is None


def test_unversioned_studio_show_is_read_as_version_1_with_a_warning(gcs, caplog):
    unversioned = _two_drone_show()
    with caplog.at_level(logging.WARNING, logger="TaramandalGCS"):
        resp = _upload(gcs, json.dumps(unversioned).encode())
    assert resp.status_code == 200, resp.text
    assert "has no 'version'" in caplog.text
    versioned = parse_trajectory_bytes(
        json.dumps({"version": SHOW_FORMAT_VERSION, **unversioned}).encode(), "show.json"
    )
    assert gcs_main.drone_manager.active_trajectory == versioned


# ----------------------------------------------------------- legacy adapters


def _skyc(show_file: dict) -> bytes:
    """A minimal Skybrush archive holding the same positions (no yaw, no light)."""
    swarm = {"drones": [
        {"trajectory": {"points": [[wp["time"], [wp["x"], wp["y"], wp["z"]], []] for wp in d["waypoints"]]}}
        for d in show_file["drones"]
    ]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("show.json", json.dumps({"swarm": swarm}))
    return buf.getvalue()


def test_legacy_formats_stage_the_same_show_as_the_canonical_file():
    canonical = {"version": 1, **_two_drone_show(r=10, g=20, b=30)}
    expected = parse_trajectory_bytes(json.dumps(canonical).encode(), "show.json")
    assert expected[0][0]["r"] == 10

    gcs_shape = {str(d["id"]): d["waypoints"] for d in canonical["drones"]}
    assert parse_trajectory_bytes(json.dumps(gcs_shape).encode(), "show.json") == expected

    # The Studio's own CSV export, LED columns included, comes back identical.
    assert parse_trajectory_bytes(export_csv(canonical).encode(), "show.csv") == expected

    # .skyc carries neither yaw nor colour; positions and timing match.
    plain = {d: [{k: wp[k] for k in ("time", "x", "y", "z")} | {"yaw": 0.0} for wp in wps]
             for d, wps in expected.items()}
    assert parse_trajectory_bytes(_skyc(canonical), "show.skyc") == plain


def test_legacy_inputs_are_normalised_but_canonical_files_are_not():
    gcs_shape = {"0": [_wp(0.0, 0.0, 0.0, yaw=360.0), _wp(6.0, 0.0, -3.0, yaw=-90.0)]}
    staged = parse_trajectory_bytes(json.dumps(gcs_shape).encode(), "show.json")
    assert [wp["yaw"] for wp in staged[0]] == [0.0, 270.0]

    csv = "drone_id,time,x,y,z,yaw,r,g,b,w\n150,0,0,0,0,-90,1,2,3,4\n150,6,0,0,-3,360,5,6,7,\n"
    staged = parse_trajectory_bytes(csv.encode(), "show.csv")
    assert list(staged) == [150]  # no more [0, 99] cap
    assert staged[150][0] == {"time": 0.0, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 270.0,
                              "r": 1, "g": 2, "b": 3, "w": 4}
    assert staged[150][1]["yaw"] == 0.0 and "w" not in staged[150][1]  # blank cell: channel omitted
    assert all(type(v) in (int, float) for wp in staged[150] for v in wp.values())

    canonical = {"version": 1, "drones": [{"id": 0, "waypoints": gcs_shape["0"]}]}
    with pytest.raises(ValueError, match=r"drones\.0\.waypoints\.0\.yaw"):
        parse_trajectory_bytes(json.dumps(canonical).encode(), "show.json")


# ------------------------------------------------------------- parity table


def _bad(mutate) -> dict:
    show = {"version": 1, **_two_drone_show(r=0, g=0, b=0)}
    mutate(show)
    return show


def _wp1(show):
    return show["drones"][1]["waypoints"][1]


BAD_SHOWS = {
    "yaw 720": (lambda s: _wp1(s).update(yaw=720.0), "drones.1.waypoints.1.yaw"),
    "yaw 360": (lambda s: _wp1(s).update(yaw=360.0), "drones.1.waypoints.1.yaw"),
    "missing yaw": (lambda s: _wp1(s).pop("yaw"), "drones.1.waypoints.1.yaw"),
    "string number": (lambda s: _wp1(s).update(x="5.0"), "drones.1.waypoints.1.x"),
    "negative time": (lambda s: _wp1(s).update(time=-1.0), "drones.1.waypoints.1.time"),
    "LED 256": (lambda s: _wp1(s).update(r=256), "drones.1.waypoints.1.r"),
    "null LED": (lambda s: _wp1(s).update(g=None), "drones.1.waypoints.1.g"),
    "float LED": (lambda s: _wp1(s).update(b=1.5), "drones.1.waypoints.1.b"),
    "NaN coordinate": (lambda s: _wp1(s).update(z=math.nan), "drones.1.waypoints.1.z"),
    "drone id 1000": (lambda s: s["drones"][1].update(id=1000), "drones.1.id"),
    "string drone id": (lambda s: s["drones"][1].update(id="1"), "drones.1.id"),
    "duplicate ids": (lambda s: s["drones"][1].update(id=0), "duplicate drone id"),
    "missing id": (lambda s: s["drones"][1].pop("id"), "drones.1.id"),
    "drones not a list": (lambda s: s.update(drones={"0": []}), "drones"),
    "version 0": (lambda s: s.update(version=0), "version"),
}


@pytest.mark.parametrize("case", list(BAD_SHOWS))
def test_what_the_studio_refuses_to_export_the_gcs_refuses_to_stage(gcs, case):
    mutate, where = BAD_SHOWS[case]
    show = _bad(mutate)

    with pytest.raises(ValidationError):
        export_json(show)

    resp = _upload(gcs, json.dumps(show).encode())
    assert resp.status_code == 400, resp.text
    assert where in resp.json()["detail"]
    assert gcs_main.drone_manager.active_trajectory is None


@pytest.mark.parametrize("show", [
    {"version": 1, "drones": []},
    {"version": 1, "drones": [{"id": 0, "waypoints": []}]},
    {},
], ids=["no drones", "empty waypoint list", "empty object"])
def test_empty_shows_are_refused(gcs, show):
    resp = _upload(gcs, json.dumps(show).encode())
    assert resp.status_code == 400, resp.text
    assert "no drones" in resp.json()["detail"] or "no waypoints" in resp.json()["detail"]


# -------------------------------------------------------------------- resume


def _resumable(tmp_path, trajectory: dict) -> DroneManager:
    path = str(tmp_path / "show_state.json")
    save_show_state(path, trajectory, "LOCAL", 0.0, {})
    manager = DroneManager(ports=[], state_path=path)
    assert manager.load_pending_resume_state() is not None
    return manager


def test_resume_restores_a_valid_snapshot_with_its_light_data(tmp_path):
    trajectory = parse_trajectory_bytes(json.dumps(_two_drone_show(r=9, g=8, b=7, w=6)).encode(), "show.json")
    manager = _resumable(tmp_path, trajectory)
    manager.resume_pending_show()
    assert manager.active_trajectory == trajectory


@pytest.mark.parametrize("tamper, reason", [
    (lambda t: t["1"][1].update(yaw=720.0), "drones.1.waypoints.1.yaw"),
    (lambda t: t["1"][1].update(r=999), "drones.1.waypoints.1.r"),
    (lambda t: t.update({"5000": t["1"]}), "drones.2.id"),
    (lambda t: t["1"].clear(), "no waypoints"),
    (lambda t: t["1"][1].update(x=0.0), "Trajectory safety check failed"),
], ids=["yaw", "LED", "drone id", "emptied", "collision"])
def test_resume_refuses_a_tampered_snapshot(tmp_path, tamper, reason):
    trajectory = parse_trajectory_bytes(json.dumps(_two_drone_show()).encode(), "show.json")
    path = tmp_path / "show_state.json"
    save_show_state(str(path), trajectory, "LOCAL", 0.0, {})
    on_disk = json.loads(path.read_text())
    tamper(on_disk["trajectory"])
    path.write_text(json.dumps(on_disk))

    manager = DroneManager(ports=[], state_path=str(path))
    assert manager.load_pending_resume_state() is not None
    with pytest.raises(ValueError, match=reason.replace(".", r"\.")) as exc:
        manager.resume_pending_show()
    assert "cannot be resumed" in str(exc.value)

    # Refused before anything was applied: the operator can still discard it.
    assert manager.active_trajectory is None
    assert manager.pending_resume_state is not None
    assert path.exists()
