"""Issue #57: the GCS authenticates its broadcast show triggers when it has a key.

End to end on the sim backend: a keyed GCS runs an onboard show through a keyed sim fleet, every START and CANCEL it
sends is an authenticated frame with a strictly increasing counter, and a forged or plain frame put on the same radio
changes nothing. The frame format and the per-drone replay rules are tested in tara-core and tara-sim.
"""

import asyncio
import os
import subprocess
import sys

import pytest

from tara_core import ShowCommand, decode_show_trigger, encode_show_trigger
from tara_core.mission import TRIGGER_AUTH_SIZE
from tara_gcs.drone_manager import DroneManager

KEY = bytes(range(32))


def _show(n: int, duration: float = 4.0) -> dict:
    return {
        d: [
            {"time": 0.0, "x": 2.2 * d, "y": 0.0, "z": -3.0, "yaw": 0.0},
            {"time": duration, "x": 2.2 * d + 1.0, "y": 0.0, "z": -3.0, "yaw": 0.0},
        ]
        for d in range(n)
    }


def _manager(tmp_path, n, key=KEY):
    return DroneManager(ports=list(range(n)), backend="sim", state_path=str(tmp_path / "state.json"), trigger_key=key)


async def _launched(manager, show):
    await manager.connect_all()
    await asyncio.sleep(0.5)
    status = await manager.upload_show(show)
    assert status["all_verified"], status
    await manager.launch_sequence()
    await asyncio.sleep(1.5)


async def _wait_for(predicate, timeout):
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.05)


def _record_frames(manager):
    frames = []
    real = manager._broadcast_transmit

    def tap(payload, target):
        frames.append(bytes(payload))
        return real(payload, target)

    manager._broadcast_transmit = tap
    return frames


@pytest.mark.asyncio
async def test_a_keyed_gcs_flies_an_onboard_show_with_authenticated_frames_only(tmp_path):
    n, show = 3, _show(3)
    manager = _manager(tmp_path, n)
    try:
        await _launched(manager, show)
        frames = _record_frames(manager)
        fs = manager._sim_fleet.state
        manager.start_trajectory(show)
        await _wait_for(lambda: fs.mission_complete.all(), timeout=10.0)

        assert frames, "no trigger was sent"
        assert all(len(f) == TRIGGER_AUTH_SIZE for f in frames)
        decoded = [decode_show_trigger(f, key=KEY) for f in frames]
        assert all(d["valid"] and d["authenticated"] for d in decoded)
        assert len({d["counter"] for d in decoded}) == 1          # one START frame, repeated until t=0
        assert not fs.offboard_active.any()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_stop_mid_show_sends_an_authenticated_cancel_with_a_newer_counter(tmp_path):
    n, show = 2, _show(2, duration=8.0)
    manager = _manager(tmp_path, n)
    try:
        await _launched(manager, show)
        frames = _record_frames(manager)
        fs = manager._sim_fleet.state
        manager.start_trajectory(show)
        await _wait_for(lambda: all(m == "SHOW" for m in fs.flight_mode), timeout=5.0)
        manager.stop_trajectory()

        decoded = [decode_show_trigger(f, key=KEY) for f in frames]
        assert decoded[-1]["command"] == ShowCommand.CANCEL and decoded[-1]["authenticated"]
        assert decoded[-1]["counter"] > max(d["counter"] for d in decoded if d["command"] == ShowCommand.START)
        assert not fs.mission_active.any() and fs.armed.all()      # the fleet holds
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_forged_and_plain_frames_on_the_same_radio_change_nothing(tmp_path):
    n, show = 2, _show(2, duration=8.0)
    manager = _manager(tmp_path, n)
    try:
        await _launched(manager, show)
        fs = manager._sim_fleet.state
        manager.start_trajectory(show)
        await _wait_for(lambda: all(m == "SHOW" for m in fs.flight_mode), timeout=5.0)
        show_id = manager.staged_show["show_id"]
        fleet = manager._sim_fleet

        fleet.broadcast(encode_show_trigger(ShowCommand.CANCEL, show_id))                                 # plain CRC
        fleet.broadcast(encode_show_trigger(ShowCommand.ESTOP, show_id))                                  # plain CRC
        fleet.broadcast(encode_show_trigger(ShowCommand.ESTOP, show_id, key=bytes(32), counter=1 << 62))  # wrong key

        assert fs.mission_active.all() and fs.armed.all()          # the show flies on
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_a_gcs_without_a_key_still_works_as_before(tmp_path):
    n, show = 2, _show(2)
    manager = _manager(tmp_path, n, key=None)
    try:
        await _launched(manager, show)
        frames = _record_frames(manager)
        fs = manager._sim_fleet.state
        manager.start_trajectory(show)
        await _wait_for(lambda: fs.mission_complete.all(), timeout=10.0)
        assert frames and all(not decode_show_trigger(f)["authenticated"] for f in frames)
    finally:
        await manager.shutdown()


def test_a_short_key_is_refused(tmp_path):
    with pytest.raises(ValueError):
        DroneManager(ports=[0], backend="sim", state_path=str(tmp_path / "s.json"), trigger_key=b"too short")


@pytest.mark.asyncio
async def test_the_counter_never_goes_backwards_even_if_the_clock_does(tmp_path, monkeypatch):
    manager = _manager(tmp_path, 1)
    sid = b"\x01" * 16
    counters = []
    for t in (1000.0, 500.0, 500.0, 2000.0):                       # a clock that steps back, then repeats
        monkeypatch.setattr("tara_gcs.drone_manager.time.time", lambda t=t: t)
        counters.append(decode_show_trigger(manager._trigger_frame(ShowCommand.CANCEL, sid), key=KEY)["counter"])
    assert counters == sorted(set(counters)) and len(set(counters)) == 4


# ------------------------------------------------------------------ startup from the environment


def _start_gcs(key: str):
    env = {**os.environ, "GCS_TRIGGER_KEY": key, "NUM_DRONES": "2", "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    code = "import tara_gcs.main as m; print('KEYED', m.drone_manager._trigger_key is not None)"
    return subprocess.run([sys.executable, "-W", "ignore", "-c", code], env=env, capture_output=True, text=True, timeout=60)


def test_a_hex_key_in_the_environment_is_used():
    done = _start_gcs("00" * 32)
    assert done.returncode == 0 and "KEYED True" in done.stdout


@pytest.mark.parametrize("bad, message", [("not-hex", "must be hex"), ("aabb", "at least 16 bytes")])
def test_a_bad_key_in_the_environment_stops_the_gcs_from_starting(bad, message):
    done = _start_gcs(bad)
    assert done.returncode != 0 and message in done.stderr
