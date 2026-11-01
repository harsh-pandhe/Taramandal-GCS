"""The flight recorder wired into the GCS (issue #65).

The integration tests start the real GCS (``with TestClient(app)`` runs the
actual startup / shutdown handlers) on a sim-backend fleet, drive it through
its HTTP routes, and read the black-box log back: telemetry, the commands that
were issued, the lifecycle transitions they caused, and - with a fault injected
through ``/api/sim/inject-fault`` - the safety breach.
"""

import asyncio
import glob
import os
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tara_gcs import blackbox_service as bbs
from tara_gcs import main as gcs_main
from tara_gcs.blackbox import FLAG_BITS, read_blackbox
from tara_gcs.blackbox_service import BlackboxService, prune_sessions
from tara_gcs.drone_manager import DroneManager
from tara_gcs.fleet_fsm import FleetStateMachine, LifecycleState as S
from tara_gcs.preflight import AuditLog


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def gcs(monkeypatch, tmp_path):
    """Install a sim-backend DroneManager (and private audit trails) into the real app."""

    def make(n: int) -> DroneManager:
        manager = DroneManager(
            ports=list(range(n)),
            backend="sim",
            state_path=str(tmp_path / "show_state.json"),
            audit_path=str(tmp_path / "fleet_audit.jsonl"),
        )
        monkeypatch.setattr(gcs_main, "drone_manager", manager)
        monkeypatch.setattr(gcs_main, "blackbox_service", None)
        monkeypatch.setattr(
            gcs_main, "command_audit", AuditLog(path=str(tmp_path / "command_audit.jsonl"), context=gcs_main._backend_audit_context)
        )
        monkeypatch.setattr(gcs_main.preflight_engine.audit, "path", str(tmp_path / "preflight_audit.jsonl"))
        return manager

    return make


def _wait(predicate, timeout=15.0, what="condition"):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def _fleet_ready(manager, n):
    return len(manager.telemetry) == n and all(
        t["connected"] and t.get("health_armable") for t in manager.telemetry.values()
    )


def _session_files(tmp_path):
    return sorted(glob.glob(str(tmp_path / "blackbox" / "blackbox_*.jsonl.gz")))


def _hover_show(duration=1.0):
    # Hover at the launch altitude (2 m) so the show moves nothing the safety monitors react to.
    return {0: [
        {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
        {"time": duration, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
    ]}


# ------------------------------------------------- the real GCS, end to end


def test_a_short_show_leaves_telemetry_commands_and_transitions_in_the_log(gcs, tmp_path):
    manager = gcs(1)
    with TestClient(gcs_main.app) as client:
        _wait(lambda: _fleet_ready(manager, 1), what="the sim fleet to connect")
        assert client.get("/api/blackbox/status").json()["recording"] is True

        assert client.post("/api/launch").status_code == 200
        _wait(lambda: manager.fleet_fsm.state is S.FORMATION_HOLD, what="takeoff to finish")
        manager.active_trajectory = _hover_show()
        res = client.post("/api/start-trajectory", json={"execution": "streamed"})
        assert res.status_code == 200, res.text
        _wait(lambda: manager.fleet_fsm.state is S.SHOW_COMPLETE, what="the show to finish")
        # A refused command is evidence too.
        assert client.post("/api/stop-trajectory").status_code == 409
        assert client.post("/api/land").status_code == 200
        _wait(lambda: manager.fleet_fsm.state is S.DISARMED, what="landing")
        status = client.get("/api/blackbox/status").json()
        assert status["ticks"] > 20 and status["errors"] == 0 and not status["failed"]
    # Leaving the context ran the real shutdown handler: the file is closed cleanly.

    files = _session_files(tmp_path)
    assert len(files) == 1
    log = read_blackbox(files[0])
    assert not log.truncated
    assert log.header["context"]["backend"] == "sim" and log.header["context"]["operating_mode"] == "SIMULATION"

    # Telemetry: the fleet climbed, armed, and came back down.
    assert all(t["ids"] == [0] for t in log.ticks)
    z_cm = [t["ch"]["z"][0] for t in log.ticks]
    armed = [bool(t["ch"]["s"][0] >> FLAG_BITS["armed"] & 1) for t in log.ticks]
    assert max(z_cm) > 150 and armed[0] is False and any(armed) and armed[-1] is False
    assert log.ticks[0]["modes"] and log.ticks[0]["fixes"]

    # Commands, in the order they were issued, with who and what came back.
    cmds = [(e["command"], e["status"], e["operator"]) for e in log.events if e["kind"] == "command"]
    assert cmds == [
        ("/api/launch", 200, "test-commander"),
        ("/api/start-trajectory", 200, "test-commander"),
        ("/api/stop-trajectory", 409, "test-commander"),
        ("/api/land", 200, "test-commander"),
    ]
    assert [e["accepted"] for e in log.events if e["kind"] == "command"] == [True, True, False, True]

    # The lifecycle transitions those commands (and the telemetry) caused.
    walk = [(e["from"], e["to"], e["trigger"]) for e in log.events if e["kind"] == "fsm_transition"]
    assert walk == [
        ("STAGED", "PREARM", "POST /api/launch"),
        ("PREARM", "ARMED", "telemetry:first_drone_armed"),
        ("ARMED", "TAKEOFF", "telemetry:climbing"),
        ("TAKEOFF", "FORMATION_HOLD", "telemetry:formation_altitude_reached"),
        ("FORMATION_HOLD", "RUNNING", "POST /api/start-trajectory"),
        ("RUNNING", "SHOW_COMPLETE", "trajectory_complete"),
        ("SHOW_COMPLETE", "LANDING", "POST /api/land"),
        ("LANDING", "DISARMED", "telemetry:all_disarmed"),
    ]
    # The pre-arm / launch decision the operator relied on is in there too.
    assert any(e["kind"] == "audit" and "launch" in str(e["audit_event"]) for e in log.events)
    # Events are in time order, and the launch command precedes its first transition.
    times = [e["t"] for e in log.events]
    assert times == sorted(times)


def _arm_all(manager):
    fs = manager._sim_fleet.state
    for i in manager.telemetry:
        fs.command_arm(i)
    _wait(lambda: all(t["armed"] for t in manager.telemetry.values()), what="drones to report armed")


def _events(tmp_path, kind):
    files = _session_files(tmp_path)
    return [e for e in read_blackbox(files[-1]).events if e["kind"] == kind] if files else []


def test_an_injected_fault_that_breaches_separation_is_recorded(gcs, tmp_path):
    manager = gcs(2)
    with TestClient(gcs_main.app) as client:
        _wait(lambda: _fleet_ready(manager, 2), what="the sim fleet to connect")
        _arm_all(manager)
        a, b = manager.telemetry[0], manager.telemetry[1]
        # Put drone 1 on top of drone 0 (offsets are in the drone's own frame; fleet = local + pad).
        offset = [a["fleet_x"] - b["fleet_x"], a["fleet_y"] - b["fleet_y"], a["fleet_z"] - b["fleet_z"]]
        res = client.post("/api/sim/inject-fault", json={"drone_id": 1, "fault_type": "deviation", "params": {"offset_m": offset}})
        assert res.status_code == 200, res.text

        # The log is readable *while the GCS is still running*: events are flushed as they happen.
        _wait(lambda: _events(tmp_path, "proximity_breach"), what="the breach to reach the log")
        log = read_blackbox(_session_files(tmp_path)[-1])
        assert log.truncated  # still open: no gzip trailer yet

    breach = [e for e in log.events if e["kind"] == "proximity_breach"][0]
    assert breach["drones"] == [0, 1] and breach["distance_m"] < 1.5
    kinds = [e["kind"] for e in log.events]
    # Cause before effect: the fault command, then the breach, then the failsafe it triggered.
    fault_cmd = next(e for e in log.events if e["kind"] == "command" and e["command"] == "/api/sim/inject-fault")
    assert fault_cmd["status"] == 200 and fault_cmd["t"] <= breach["t"]
    rtl = [e for e in log.events if e["kind"] == "fsm_transition" and e["trigger"] == "failsafe:proximity"]
    assert rtl and rtl[0]["t"] >= breach["t"], kinds
    # And the telemetry shows the two drones on top of each other at the time.
    last = log.ticks[-1]
    assert abs(last["ch"]["x"][0] - last["ch"]["x"][1]) < 150


def test_an_injected_fault_that_leaves_the_geofence_is_recorded(gcs, tmp_path):
    manager = gcs(1)
    with TestClient(gcs_main.app) as client:
        _wait(lambda: _fleet_ready(manager, 1), what="the sim fleet to connect")
        _arm_all(manager)
        res = client.post("/api/sim/inject-fault", json={"drone_id": 0, "fault_type": "deviation", "params": {"offset_m": [900.0, 0.0, 0.0]}})
        assert res.status_code == 200
        _wait(lambda: _events(tmp_path, "geofence_hard_breach"), what="the geofence breach to reach the log")
    breach = _events(tmp_path, "geofence_hard_breach")[0]
    assert breach["drone"] == 0 and breach["distance_m"] > 800 and breach["limit_m"] == manager.geofence_radius
    assert breach["limit_m"] < breach["distance_m"]


# ----------------------------------------------------- configuration, safety


def test_it_is_on_by_default_and_off_with_the_switch(gcs, tmp_path, monkeypatch):
    gcs(1)
    with TestClient(gcs_main.app) as client:
        s = client.get("/api/blackbox/status").json()
        assert s["enabled"] and s["recording"] and s["directory"] == str(tmp_path / "blackbox")
    assert len(_session_files(tmp_path)) == 1

    monkeypatch.setenv("GCS_BLACKBOX", "0")
    other = tmp_path / "off"
    monkeypatch.setenv("GCS_BLACKBOX_DIR", str(other))
    gcs(1)
    with TestClient(gcs_main.app) as client:
        assert client.get("/api/blackbox/status").json() == {"enabled": False, "recording": False}
    assert not other.exists()


def test_bad_settings_never_stop_the_gcs_from_starting(gcs, tmp_path, monkeypatch, caplog):
    gcs(1)
    monkeypatch.setenv("GCS_BLACKBOX_MAX_MB", "lots")
    with TestClient(gcs_main.app) as client:
        assert client.get("/api/status").status_code == 200
        assert client.get("/api/blackbox/status").json()["enabled"] is False
    assert "settings are invalid" in caplog.text

    # A directory that cannot be created: the recorder is skipped, the GCS runs.
    monkeypatch.delenv("GCS_BLACKBOX_MAX_MB")
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    monkeypatch.setenv("GCS_BLACKBOX_DIR", str(blocker / "sub"))
    gcs(1)
    with TestClient(gcs_main.app) as client:
        assert client.get("/api/status").status_code == 200
        assert client.get("/api/blackbox/status").json()["recording"] is False
    assert "runs without a flight recorder" in caplog.text


# --------------------------------------------------------- service behaviour


class FakeDM:
    """The public surface BlackboxService reads from a DroneManager."""

    def __init__(self, n=100, seed=0):
        self.rng = np.random.default_rng(seed)
        self.n = n
        self.telemetry = {
            i: {"connected": True, "armed": True, "flight_mode": "OFFBOARD", "gps_fix_type": "FIX_3D", "fleet_x": 0.0,
                "fleet_y": 0.0, "fleet_z": 10.0, "local_x": 0.0, "local_y": 0.0, "battery_percent": 90.0}
            for i in range(n)
        }
        self.fleet_fsm = FleetStateMachine(audit_path=None)
        self.proximity_breaches: dict = {}
        self.geofence_breaches: dict = {}
        self.geofence_warnings: dict = {}
        self.geofence_radius = 100.0
        self.soft_geofence_radius = None

    def jitter(self):
        for t in self.telemetry.values():
            t["fleet_x"] = round(float(self.rng.normal(0, 20)), 2)
            t["fleet_y"] = round(float(self.rng.normal(0, 20)), 2)

    def rf_channel_drop_rate(self):
        return None


def test_prune_deletes_the_oldest_and_never_the_file_in_use(tmp_path):
    names = []
    for i in range(5):
        p = tmp_path / f"blackbox_2026110{i}T000000_1.jsonl.gz"
        p.write_bytes(b"x" * 1000)
        os.utime(p, (1_000_000 + i, 1_000_000 + i))
        names.append(str(p))
    (tmp_path / "unrelated.txt").write_bytes(b"y" * 5000)  # not a session file: never touched
    removed = prune_sessions(str(tmp_path), 2500, keep=names[0])  # the oldest is the one in use
    assert removed == names[1:4]
    assert os.path.exists(names[0]) and os.path.exists(names[4]) and (tmp_path / "unrelated.txt").exists()


async def test_session_files_rotate_and_old_ones_are_pruned(tmp_path):
    dm = FakeDM(n=100)
    svc = BlackboxService(dm, directory=str(tmp_path), max_total_mb=0.04, file_mb=0.01, recorder_kwargs={"fsync_interval_s": None, "flush_interval_s": 0.1})
    svc.start()
    try:
        for _ in range(400):  # noisy data, so files grow quickly
            dm.jitter()
            await asyncio.sleep(0.05)
            if svc.sessions >= 6:
                break
        assert svc.sessions >= 6, svc.status()
    finally:
        await svc.stop()
    files = sorted(glob.glob(str(tmp_path / "blackbox_*.jsonl.gz")))
    sizes = [os.path.getsize(f) for f in files]
    assert len(files) < svc.sessions  # older sessions were deleted
    assert sum(sizes) <= svc.max_total_bytes + svc.file_bytes  # the cap held (one file of slack for the live one)
    for f in files[:-1]:  # every kept session decodes; the ones rotated out were closed cleanly
        log = read_blackbox(f)
        assert log.header["format"] == "tara-blackbox" and log.ticks and not log.truncated
    assert read_blackbox(files[-1]).header["format"] == "tara-blackbox"  # newest may have just been opened


async def test_a_disk_failure_is_swallowed_and_a_fresh_file_takes_over(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(bbs, "RETRY_AFTER_FAILURE_S", 0.2)
    dm = FakeDM(n=5)
    svc = BlackboxService(dm, directory=str(tmp_path), recorder_kwargs={"fsync_interval_s": None})
    svc.start()
    try:
        await asyncio.sleep(0.4)
        first = svc.recorder

        def full_disk(*_a, **_k):
            raise OSError(28, "No space left on device")

        first._file.flush = full_disk
        # A safety event during the failure: must not raise into the monitor that reports it.
        dm.proximity_breaches[(0, 1)] = True
        await asyncio.sleep(0.3)
        assert first.failed
        dm.proximity_breaches[(0, 1)] = False
        await asyncio.sleep(0.6)  # past the retry delay
        assert svc.recorder is not first and not svc.recorder.failed and svc.sessions == 2
        dm.geofence_breaches[3] = True
        await asyncio.sleep(0.2)
    finally:
        await svc.stop()
    assert sum("recording stopped" in r.message for r in caplog.records) == 1
    second = read_blackbox(svc_files(tmp_path)[-1])
    assert any(e["kind"] == "geofence_hard_breach" and e["drone"] == 3 for e in second.events)
    assert second.ticks


def svc_files(tmp_path):
    return sorted(glob.glob(str(tmp_path / "blackbox_*.jsonl.gz")))


async def test_a_breach_that_clears_is_recorded_both_ways(tmp_path):
    dm = FakeDM(n=3)
    svc = BlackboxService(dm, directory=str(tmp_path), recorder_kwargs={"fsync_interval_s": None})
    svc.start()
    try:
        await asyncio.sleep(0.15)
        dm.proximity_breaches[(2, 0)] = True
        await asyncio.sleep(0.2)
        dm.proximity_breaches[(2, 0)] = False
        dm.geofence_warnings[1] = True
        dm.soft_geofence_radius = 50.0
        await asyncio.sleep(0.2)
    finally:
        await svc.stop()
    kinds = [e["kind"] for e in read_blackbox(svc_files(tmp_path)[0]).events]
    assert kinds == ["proximity_breach", "proximity_clear", "geofence_soft_breach"]


def test_a_failing_observer_cannot_break_the_audit_trail_or_a_transition(caplog):
    audit = AuditLog(path=None)
    fsm = FleetStateMachine(audit_path=None)

    def boom(_entry):
        raise RuntimeError("recorder bug")

    audit.observers.append(boom)
    fsm.observers.append(boom)
    entry = audit.record("launch", operator="alice")
    rec = fsm.transition(S.PREARM, operator="alice", trigger="t")
    assert entry["event"] == "launch" and list(audit.recent) == [entry]
    assert rec["to"] == "PREARM" and fsm.state is S.PREARM
    assert caplog.text.count("observer failed") == 2


async def test_audit_fields_cannot_overwrite_the_log_framing(tmp_path):
    dm = FakeDM(n=2)
    audit = AuditLog(path=None)
    svc = BlackboxService(dm, audit_logs=(audit,), directory=str(tmp_path), recorder_kwargs={"fsync_interval_s": None})
    svc.start()
    try:
        audit.record("weird", type="evil", t=-1, kind="x", wall=0)
    finally:
        await svc.stop()
    event = read_blackbox(svc_files(tmp_path)[0]).events[0]
    assert event["kind"] == "audit" and event["type"] == "event" and event["t"] >= 0
    assert (event["field_type"], event["field_t"], event["field_kind"]) == ("evil", -1, "x")


# ------------------------------------------------------------ cost, wired in


@pytest.mark.slow
async def test_wired_in_it_does_not_slow_the_gcs_tick_at_100_drones(tmp_path):
    """A real 100-drone sim DroneManager (telemetry, proximity, geofence and lifecycle
    loops all running) with a 10 Hz probe measuring event-loop lateness, once
    without and once with the recorder service on it."""
    n = 100

    async def lateness(with_recorder: bool):
        manager = DroneManager(ports=list(range(n)), backend="sim", sim_rng_seed=1, audit_path=None, state_path=str(tmp_path / "s.json"))
        await manager.connect_all()
        for i in range(n):
            manager._sim_fleet.state.command_arm(i)
        svc = None
        if with_recorder:
            svc = BlackboxService(manager, directory=str(tmp_path / "bb"))
            polls = []
            real_poll = svc._poll

            async def timed(now):
                t = time.perf_counter()
                await real_poll(now)
                polls.append(time.perf_counter() - t)

            svc._poll = timed
            svc.start()
        try:
            await asyncio.sleep(1.0)  # settle
            loop = asyncio.get_running_loop()
            late = []
            start = loop.time()
            for k in range(80):
                due = start + k * 0.1
                await asyncio.sleep(max(0.0, due - loop.time()))
                late.append(loop.time() - due)
            return np.mean(late) * 1e3, np.percentile(late, 99) * 1e3, (np.array(polls) * 1e3 if with_recorder else None), svc
        finally:
            if svc:
                await svc.stop()
            await manager.shutdown()

    base_mean, base_p99, _, _ = await lateness(False)
    mean, p99, polls, svc = await lateness(True)
    print(f"\nN={n}: tick lateness mean {base_mean:.3f} -> {mean:.3f} ms, p99 {base_p99:.3f} -> {p99:.3f} ms; "
          f"service poll (every 50 ms, a tick every other one) mean {polls.mean():.3f} ms, "
          f"p99 {np.percentile(polls, 99):.3f} ms, max {polls.max():.2f} ms over {len(polls)} polls")
    assert svc.status()["errors"] == 0
    assert mean - base_mean < 5.0
    assert np.percentile(polls, 99) < 20.0
