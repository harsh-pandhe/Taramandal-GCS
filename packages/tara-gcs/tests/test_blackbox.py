"""Black-box recorder (issue #65, V1 slice).

The three acceptance criteria this slice owns are each proven with a real run,
not a calculation:

  * a 15-minute, 100-drone log is under 25 MB - driven by the real tara_sim
    fleet through DroneManager's own telemetry ingest, so the dicts, the sensor
    noise and the RF drop rates are the production ones;
  * the log survives a GCS kill - a child process is SIGKILLed with the
    recorder still open and the file is read back;
  * recording does not slow the 10 Hz tick - the per-call cost is measured at
    N=100 and the loop's lateness is compared with and without the recorder.
"""

import asyncio
import math
import os
import signal
import subprocess
import sys
import textwrap
import time

import numpy as np
import pytest

from tara_gcs import blackbox as bb
from tara_gcs.blackbox import BlackboxRecorder, read_blackbox

GCS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, dt=0.1):
        self.t += dt


def _tel(i, k, **over):
    """One drone's telemetry dict with the fields DroneManager keeps."""
    tel = {
        "id": i, "connected": True, "armed": True, "flight_mode": "OFFBOARD",
        "battery_percent": round(100 - 0.01 * k, 1), "battery_voltage": 16.0, "gps_lock": True,
        "satellites": 16, "gps_fix_type": "FIX_3D", "comm_latency_ms": 0.0,
        "fleet_x": round(i + 0.05 * k, 2), "fleet_y": round(2.0 * i - 0.03 * k, 2), "fleet_z": round(10 + 0.01 * k, 2),
        "heading": 0.0, "planned_valid": True, "aborted": None,
    }
    tel.update(over)
    return tel


def _fleet(n, k, **over):
    return {i: _tel(i, k, **over) for i in range(n)}


@pytest.fixture
def clock():
    return FakeClock()


def _rec(tmp_path, clock, **kw):
    kw.setdefault("fsync_interval_s", None)
    return BlackboxRecorder(str(tmp_path / "b.jsonl.gz"), clock=clock, wall_clock=lambda: 1_700_000_000.0, **kw)


# ---------------------------------------------------------------- round trip


def test_round_trip_is_exact_across_keyframes(tmp_path, clock):
    rng = np.random.default_rng(0)
    rec = _rec(tmp_path, clock, keyframe_interval_s=2.0)
    expected = []
    for k in range(120):
        fleet = {
            i: _tel(i, k, fleet_x=round(float(rng.normal(0, 5)), 2), fleet_y=round(float(rng.normal(0, 5)), 2),
                    battery_percent=round(float(rng.uniform(0, 100)), 1), satellites=int(rng.integers(0, 20)),
                    armed=bool(rng.integers(0, 2)), comm_latency_ms=float(rng.integers(0, 50)))
            for i in range(7)
        }
        rf = {i: round(float(rng.uniform(0, 1)), 4) for i in range(7)}
        assert rec.record_tick(fleet, rf_drop_rate=rf)
        expected.append((fleet, rf))
        clock.advance()
    rec.close()

    log = read_blackbox(rec.path)
    assert not log.truncated and log.header["format"] == "tara-blackbox"
    assert len(log.ticks) == 120 and sum(t["key"] for t in log.ticks) > 3  # several keyframes crossed
    for tick, (fleet, rf) in zip(log.ticks, expected):
        assert tick["ids"] == list(range(7))
        for j, i in enumerate(tick["ids"]):
            assert tick["ch"]["x"][j] == round(fleet[i]["fleet_x"] * 100)
            assert tick["ch"]["y"][j] == round(fleet[i]["fleet_y"] * 100)
            assert tick["ch"]["b"][j] == round(fleet[i]["battery_percent"] * 10)
            assert tick["ch"]["n"][j] == fleet[i]["satellites"]
            assert tick["ch"]["l"][j] == fleet[i]["comm_latency_ms"]
            assert tick["ch"]["r"][j] == round(rf[i] * 10000)
            assert bool(tick["ch"]["s"][j] >> bb.FLAG_BITS["armed"] & 1) == fleet[i]["armed"]
    ts, xs = log.series(3, "x")
    assert xs[5] == expected[5][0][3]["fleet_x"] and ts[5] == 500


def test_drone_set_and_dictionary_changes_force_a_keyframe(tmp_path, clock):
    rec = _rec(tmp_path, clock, keyframe_interval_s=1e9)
    rec.record_tick(_fleet(3, 0))
    clock.advance()
    rec.record_tick(_fleet(3, 1))
    clock.advance()
    rec.record_tick(_fleet(4, 2))
    clock.advance()  # a fourth drone connects
    rec.record_tick(_fleet(4, 3, flight_mode="HOLD"))
    clock.advance()  # new flight-mode string
    rec.record_tick(_fleet(4, 4, flight_mode="HOLD"))
    rec.close()
    log = read_blackbox(rec.path)
    assert [t["key"] for t in log.ticks] == [True, False, True, True, False]
    last = log.ticks[-1]
    assert [last["modes"][m] for m in last["ch"]["m"]] == ["HOLD"] * 4
    assert last["fixes"] == ["FIX_3D"]


def test_missing_and_nonfinite_values_are_recorded_not_raised(tmp_path, clock):
    rec = _rec(tmp_path, clock)
    fleet = _fleet(2, 0)
    fleet[0].update(fleet_x=float("nan"), battery_voltage=None, heading=float("inf"))
    assert rec.record_tick(fleet, rf_drop_rate=None)
    rec.close()
    tick = read_blackbox(rec.path).ticks[0]
    assert tick["ch"]["x"][0] == 0 and tick["ch"]["v"][0] == 0 and tick["ch"]["h"][0] == 0
    assert tick["ch"]["r"] == [bb.MISSING, bb.MISSING]  # no RF model: distinguishable from 0% loss
    assert rec.errors == 0


def test_scalar_rf_figure_applies_to_every_drone(tmp_path, clock):
    rec = _rec(tmp_path, clock)
    rec.record_tick(_fleet(3, 0), rf_drop_rate=0.0123)
    rec.close()
    assert read_blackbox(rec.path).ticks[0]["ch"]["r"] == [123, 123, 123]


def test_ticks_faster_than_the_rate_are_dropped(tmp_path, clock):
    rec = _rec(tmp_path, clock)
    assert rec.record_tick(_fleet(2, 0))
    clock.advance(0.01)
    assert not rec.record_tick(_fleet(2, 1))  # a second loop calling in the same period
    clock.advance(0.095)  # ~10 Hz with jitter still passes
    assert rec.record_tick(_fleet(2, 2))
    rec.close()
    assert len(read_blackbox(rec.path).ticks) == 2


# -------------------------------------------------------------------- events


def test_events_are_recorded_with_context_and_in_order(tmp_path, clock):
    rec = _rec(tmp_path, clock, context=lambda: {"backend": "sim", "fidelity": "kinematic"})
    rec.record_tick(_fleet(2, 0))
    clock.advance()
    rec.record_command("launch", operator="alice", trigger="api:/launch", accepted=True, drones=[0, 1])
    rec.record_fsm_transition({"seq": 1, "ts": 1.0, "time": "x", "accepted": True, "from": "STAGED", "to": "ARMED",
                               "operator": "alice", "trigger": "api"})
    rec.record_fsm_transition({"ts": 2.0, "time": "y", "accepted": False, "command": "start", "state": "STAGED",
                               "operator": "bob", "trigger": "api", "reason": "illegal"})
    rec.record_proximity_breach((3, 1), 0.8123456)
    rec.record_proximity_breach((1, 3), 2.0, cleared=True)
    rec.record_geofence(4, "hard", 91.0, 90.0)
    rec.record_geofence(4, "soft", 70.0, 60.0, cleared=True)
    rec.close()
    log = read_blackbox(rec.path)
    kinds = [e["kind"] for e in log.events]
    assert kinds == ["command", "fsm_transition", "fsm_rejected", "proximity_breach", "proximity_clear",
                     "geofence_hard_breach", "geofence_soft_clear"]
    assert all(e["backend"] == "sim" and e["fidelity"] == "kinematic" for e in log.events)
    assert log.events[0]["operator"] == "alice" and log.events[0]["drones"] == [0, 1]
    assert log.events[3]["distance_m"] == 0.812 and log.events[3]["drones"] == [3, 1]
    assert log.events[2]["reason"] == "illegal" and "ts" not in log.events[2]
    assert log.header["context"] == {"backend": "sim", "fidelity": "kinematic"}
    assert all(e["t"] == 100 for e in log.events)  # ms since the recorder started


def test_a_failing_context_provider_does_not_lose_the_event(tmp_path, clock):
    def boom():
        raise RuntimeError("no context")

    rec = _rec(tmp_path, clock)
    rec.context = boom
    rec.record_event("command", command="x")
    rec.close()
    assert [e["kind"] for e in read_blackbox(rec.path).events] == ["command"]


# ---------------------------------------------------------------- durability


def test_log_is_readable_while_the_recorder_is_still_open(tmp_path, clock):
    rec = _rec(tmp_path, clock, flush_interval_s=1.0)
    for k in range(25):  # 2.5 s of ticks: two flush points crossed
        rec.record_tick(_fleet(5, k))
        clock.advance()
    log = read_blackbox(rec.path)  # recorder NOT closed
    assert log.truncated  # no gzip trailer yet
    assert 20 <= len(log.ticks) <= 25  # at most the last flush interval is outstanding
    rec.close()
    assert not read_blackbox(rec.path).truncated


def test_events_are_on_disk_immediately(tmp_path, clock):
    rec = _rec(tmp_path, clock, flush_interval_s=1e9)
    rec.record_tick(_fleet(3, 0))
    clock.advance()
    rec.record_geofence(2, "hard", 120.0, 100.0)
    log = read_blackbox(rec.path)  # no flush, no close
    assert [e["kind"] for e in log.events] == ["geofence_hard_breach"]
    assert len(log.ticks) == 1  # the event's flush carried the preceding tick with it
    rec.close()


def test_truncated_file_yields_its_decodable_prefix(tmp_path, clock):
    rng = np.random.default_rng(1)
    rec = _rec(tmp_path, clock)
    for k in range(1000):
        fleet = _fleet(60, k, fleet_x=round(float(rng.normal(0, 5)), 2))  # noisy: does not compress to nothing
        rec.record_tick(fleet, rf_drop_rate=0.01)
        clock.advance()
    rec.record_event("command", command="rtl")
    rec.close()
    raw = open(rec.path, "rb").read()
    assert len(raw) > 5 * bb._READ_SLICE
    full = read_blackbox(rec.path)
    assert not full.truncated and len(full.ticks) == 1000

    for cut in (len(raw) // 2, len(raw) - 1, len(raw) - 8):
        cut_path = tmp_path / f"cut{cut}.jsonl.gz"
        cut_path.write_bytes(raw[:cut])
        log = read_blackbox(str(cut_path))
        assert log.truncated and log.header["version"] == 1
        # Losing only the gzip trailer (the SIGKILL case) loses no ticks; losing half the file loses half.
        assert 0 < len(log.ticks) <= 1000
        assert (len(log.ticks) < 1000) == (cut == len(raw) // 2)
        # What did decode is exactly what was written, not a plausible-looking guess.
        assert [t["ch"]["x"] for t in log.ticks] == [t["ch"]["x"] for t in full.ticks[: len(log.ticks)]]

    # Mid-stream corruption stops at the damage instead of raising, keeping everything before it.
    bad = bytearray(raw)
    bad[len(bad) * 3 // 4] ^= 0xFF
    (tmp_path / "bad.jsonl.gz").write_bytes(bytes(bad))
    log = read_blackbox(str(tmp_path / "bad.jsonl.gz"))
    assert log.truncated and 300 < len(log.ticks) < 1000


def test_not_a_blackbox_file_is_rejected(tmp_path):
    import gzip

    p = tmp_path / "x.jsonl.gz"
    with gzip.open(p, "wt") as f:
        f.write('{"type":"event"}\n')
    with pytest.raises(ValueError, match="no header"):
        read_blackbox(str(p))


def test_never_overwrites_an_earlier_session(tmp_path, clock):
    rec = _rec(tmp_path, clock)
    rec.close()
    with pytest.raises(FileExistsError):
        _rec(tmp_path, clock)


def test_new_session_names_are_unique_per_directory(tmp_path):
    rec = BlackboxRecorder.new_session(str(tmp_path / "bb"), fsync_interval_s=None)
    rec.close()
    assert os.path.basename(rec.path).startswith("blackbox_") and rec.path.endswith(".jsonl.gz")


def test_write_failure_is_contained_and_counted(tmp_path, clock, caplog):
    rec = _rec(tmp_path, clock)
    assert rec.record_tick(_fleet(3, 0))
    clock.advance()

    def full_disk(*_a, **_k):
        raise OSError(28, "No space left on device")

    rec._file.flush = full_disk  # the OS write is what fails
    rec.record_event("command", command="rtl")  # must not raise into a safety loop
    assert rec.failed
    clock.advance()
    assert not rec.record_tick(_fleet(3, 1))
    rec.record_event("command", command="again")
    assert rec.dropped >= 3
    assert sum("recording stopped" in r.message for r in caplog.records) == 1  # logged once, not per tick
    rec.close()


def test_fsync_thread_runs_and_stops_cleanly(tmp_path, clock):
    rec = BlackboxRecorder(str(tmp_path / "f.jsonl.gz"), fsync_interval_s=0.05, clock=clock)
    rec.record_event("command", command="x")
    time.sleep(0.2)
    assert rec._fsync_thread.is_alive() and not rec._dirty  # it synced what the event wrote
    rec.close()
    assert not rec._fsync_thread.is_alive()


def test_log_survives_a_sigkill(tmp_path):
    """A child records ticks and an incident event, then is SIGKILLed with the
    recorder open - no close(), no atexit, no gzip trailer."""
    path = tmp_path / "killed.jsonl.gz"
    child = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {GCS_ROOT!r})
        from tara_gcs.blackbox import BlackboxRecorder
        t = [0.0]
        rec = BlackboxRecorder({str(path)!r}, clock=lambda: t[0], flush_interval_s=1e9, fsync_interval_s=0.05)
        def fleet(k):
            return {{i: {{"connected": True, "armed": True, "fleet_x": i + 0.1 * k, "fleet_y": 0.0, "fleet_z": 10.0,
                         "battery_percent": 90.0, "flight_mode": "OFFBOARD", "gps_fix_type": "FIX_3D"}} for i in range(20)}}
        for k in range(30):
            rec.record_tick(fleet(k)); t[0] += 0.1
        rec.record_geofence(7, "hard", 130.0, 100.0)      # the incident: flushed on the spot
        for k in range(30, 33):                           # a few ticks after it, inside the unflushed window
            rec.record_tick(fleet(k)); t[0] += 0.1
        print("READY", flush=True)
        time.sleep(60)
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "READY"
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == -signal.SIGKILL

    log = read_blackbox(str(path))
    assert log.truncated  # killed: no trailer
    assert [e["kind"] for e in log.events] == ["geofence_hard_breach"] and log.events[0]["drone"] == 7
    assert 30 <= len(log.ticks) <= 33  # everything up to the incident, at most the last window after it lost
    assert log.ticks[29]["ch"]["x"][5] == round((5 + 0.1 * 29) * 100)


# -------------------------------------------------------------- cost / budget


def test_record_tick_cost_at_100_drones_is_a_small_fraction_of_the_tick(tmp_path):
    rec = BlackboxRecorder(str(tmp_path / "c.jsonl.gz"), fsync_interval_s=None, clock=FakeClock())
    clock = rec._clock
    costs = []
    for k in range(600):
        fleet = _fleet(100, k)
        t = time.perf_counter()
        rec.record_tick(fleet, rf_drop_rate=0.01)
        costs.append(time.perf_counter() - t)
        clock.advance()
    rec.close()
    mean_ms, p99_ms = np.mean(costs) * 1e3, np.percentile(costs, 99) * 1e3
    # The tick is 100 ms. Budget: mean under 5%, p99 (which includes keyframes and flushes) under 20%.
    assert mean_ms < 5.0, f"mean {mean_ms:.2f} ms"
    assert p99_ms < 20.0, f"p99 {p99_ms:.2f} ms"


# ------------------------------------------------------- real-fleet acceptance


async def _sim_fleet(n, **kw):
    """A DroneManager on the sim backend with its own loops stopped, so the test
    can drive physics and telemetry ingest in virtual time."""
    from tara_gcs.drone_manager import DroneManager

    dm = DroneManager(ports=list(range(n)), backend="sim", sim_rng_seed=1, **kw)
    await dm.connect_all()
    for t in dm._tasks:
        t.cancel()
    await asyncio.gather(*dm._tasks, return_exceptions=True)
    dm._tasks.clear()
    fs = dm._sim_fleet.state
    for i in range(n):
        fs.command_arm(i)
        fs.command_offboard_start(i)
    return dm, fs


def _fly_one_tick(dm, fs, ph, t, k):
    """Advance the real physics 0.1 s along a smooth per-drone Lissajous and refresh telemetry."""
    w = 0.15
    for i in range(len(ph)):
        p = (15 * math.sin(w * t + ph[i]), 15 * math.cos(0.8 * w * t + ph[i]), 10 + 3 * math.sin(0.2 * t + ph[i]))
        v = (15 * w * math.cos(w * t + ph[i]), -12 * w * math.sin(0.8 * w * t + ph[i]), 0.6 * math.cos(0.2 * t + ph[i]))
        fs.command_setpoint(i, p, v)
    for _ in range(5):
        fs.tick(0.02)
    dm._ingest_bulk_telemetry(dm._sim_fleet.get_bulk_telemetry(), include_slow=(k % 10 == 0))


@pytest.mark.slow
async def test_15_minute_100_drone_log_is_under_25_mb(tmp_path):
    n, minutes = 100, 15
    dm, fs = await _sim_fleet(n, sim_packet_loss_base=0.01, sim_packet_loss_per_100m=0.02)
    clock = FakeClock()
    rec = BlackboxRecorder(str(tmp_path / "show.jsonl.gz"), clock=clock, fsync_interval_s=None,
                           context=lambda: {"backend": dm.backend, "fidelity": dm.fidelity})
    ph = np.arange(n) * 0.37
    ticks = minutes * 60 * 10
    try:
        for k in range(ticks):
            _fly_one_tick(dm, fs, ph, k * 0.1, k)
            rec.record_tick(dm.telemetry, rf_drop_rate=fs.rf_channel.drop_rate())
            if k % 600 == 300:  # an event every minute, so they are part of the measured size
                rec.record_command("hold", operator="test", trigger="bench")
            clock.advance()
    finally:
        rec.close()
        await dm.shutdown()

    size = os.path.getsize(rec.path)
    print(f"\n15 min x {n} drones: {rec.ticks} ticks, {size / 1e6:.2f} MB")
    assert rec.ticks == ticks and rec.errors == 0 and not rec.failed
    assert size < 25 * 1024 * 1024

    log = read_blackbox(rec.path)  # and it decodes, end to end
    assert not log.truncated and len(log.ticks) == ticks and len(log.events) == minutes
    last = log.ticks[-1]
    assert last["ids"] == list(range(n))
    assert last["ch"]["x"][0] == round(dm.telemetry[0]["fleet_x"] * 100)
    assert log.header["context"]["backend"] == "sim"


@pytest.mark.slow
async def test_recording_does_not_slow_a_10hz_loop(tmp_path):
    """The same 10 Hz asyncio loop at N=100, with and without the recorder in
    it: mean lateness must not move by more than a few ms."""
    n, ticks = 100, 60

    async def run(recorder):
        loop = asyncio.get_running_loop()
        late = []
        start = loop.time()
        telemetry = _fleet(n, 0)
        for k in range(ticks):
            due = start + k * 0.1
            await asyncio.sleep(max(0.0, due - loop.time()))
            late.append(loop.time() - due)
            telemetry = _fleet(n, k)  # the loop's own work, identical in both runs
            if recorder is not None:
                recorder.record_tick(telemetry, rf_drop_rate=0.01)
        return np.mean(late) * 1e3

    base = await run(None)
    rec = BlackboxRecorder(str(tmp_path / "loop.jsonl.gz"), fsync_interval_s=5.0)
    try:
        with_rec = await run(rec)
    finally:
        rec.close()
    assert rec.ticks == ticks
    print(f"\nmean tick lateness: {base:.3f} ms without, {with_rec:.3f} ms with the recorder")
    assert with_rec - base < 5.0
