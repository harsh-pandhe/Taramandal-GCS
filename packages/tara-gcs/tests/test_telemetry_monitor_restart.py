"""Telemetry monitors must survive a dropped mavsdk_server stream (issue #171).

Seen on real PX4 SITL: ``pkill -9 mavsdk_server`` against a running GCS ended
every telemetry monitor for good, and ``connected`` stayed 0 until the GCS was
restarted. Probing the real binary shows what a drop looks like: the open stream
raises ``AioRpcError(UNAVAILABLE)``, and with the server gone a new subscribe
raises the same at once, so retrying alone never recovers; the server has to
come back too.

While a stream is down the last values in the telemetry dict are history, so the
drone must not read as fresh: a position with no data behind it, or an armed
flag that can no longer change, is what these tests refuse.
"""

import asyncio
import signal
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import grpc
import pytest
from grpc.aio import AioRpcError, Metadata

from tara_gcs import drone_manager as dm
from tara_gcs import main as gcs_main
from tara_gcs.drone_manager import DroneManager

sys.path.insert(0, str(Path(__file__).parent))
from mavlink_heartbeat import HeartbeatSource  # noqa: E402

_END = object()


def _unavailable() -> AioRpcError:
    return AioRpcError(grpc.StatusCode.UNAVAILABLE, Metadata(), Metadata(), details="Stream removed (Socket closed)")


class _Stream:
    """One MAVSDK telemetry stream a test can feed, drop or end."""

    def __init__(self):
        self.subscribes = 0
        self.refusing = False
        self._current: asyncio.Queue | None = None
        self.pending: list = []

    def __call__(self):
        self.subscribes += 1
        return self._iterate()

    async def _iterate(self):
        if self.refusing:
            raise _unavailable()
        queue = self._current = asyncio.Queue()
        for item in self.pending:
            queue.put_nowait(item)
        self.pending = []
        while True:
            item = await queue.get()
            if item is _END:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    def push(self, item) -> None:
        if self._current is None:
            self.pending.append(item)
        else:
            self._current.put_nowait(item)


def _position(north: float = 0.0) -> SimpleNamespace:
    return SimpleNamespace(position=SimpleNamespace(north_m=north, east_m=0.0, down_m=-1.0))


#: name on FakeSystem -> a sample that stream's monitor accepts.
_SAMPLES = {
    "connection_state": SimpleNamespace(is_connected=True),
    "battery": SimpleNamespace(remaining_percent=90.0, voltage_v=16.0),
    "flight_mode": "HOLD",
    "gps_info": SimpleNamespace(num_satellites=10, fix_type="FIX_3D"),
    "position_velocity_ned": _position(),
    "armed": False,
    "landed_state": "ON_GROUND",
    "home": SimpleNamespace(latitude_deg=47.0, longitude_deg=8.0, absolute_altitude_m=400.0),
    "health": SimpleNamespace(is_armable=True, is_home_position_ok=True),
}


class _FakeProcess:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode


class _FakeSystem:
    def __init__(self, port=None):
        self.port = port
        self.streams = {name: _Stream() for name in _SAMPLES}
        self.core = SimpleNamespace(connection_state=self.streams["connection_state"])
        self.telemetry = SimpleNamespace(**{n: s for n, s in self.streams.items() if n != "connection_state"})
        # Only the latency probe may touch action: restarting a stream must never command the vehicle.
        self.action = SimpleNamespace(get_takeoff_altitude=self._takeoff_altitude)
        self.connects: list[str] = []
        self._server_process = _FakeProcess()

    async def _takeoff_altitude(self):
        return 2.5

    async def connect(self, system_address=None):
        # Like MAVSDK: stopping the old server resets the System, so the
        # process is None for a moment before the new one is up.
        self.connects.append(system_address)
        self._server_process = None
        await asyncio.sleep(0.2)
        self._server_process = _FakeProcess()
        for stream in self.streams.values():
            stream.refusing = False

    def all_report(self) -> None:
        for name, stream in self.streams.items():
            stream.push(_SAMPLES[name])


def _use_fake_system(monkeypatch) -> None:
    # The class _connect_drone builds: GcsBoundSystem once the lifeline (#171)
    # is in, plain System before.
    name = "GcsBoundSystem" if hasattr(dm, "GcsBoundSystem") else "System"
    monkeypatch.setattr(dm, name, _FakeSystem)


async def _until(predicate, timeout_s: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return bool(predicate())


@pytest.fixture
async def fleet(monkeypatch):
    """A px4-backend DroneManager over a fake System, every stream reporting."""
    _use_fake_system(monkeypatch)
    manager = DroneManager(ports=[14540], backend="px4")
    monkeypatch.setattr(gcs_main, "drone_manager", manager)
    await manager.connect_all()
    drone = manager.drones[0]
    drone.connects.clear()  # only reconnects count from here on
    drone.all_report()
    tel = manager.telemetry[0]
    assert await _until(lambda: tel["landed_state"] == "ON_GROUND" and tel["connected"] and tel["local_z"] == 1.0)
    yield manager, drone, tel
    await manager.shutdown()


@pytest.mark.parametrize("name", list(_SAMPLES))
async def test_every_monitor_resubscribes_after_an_rpc_error(fleet, name):
    _, drone, _ = fleet
    stream = drone.streams[name]
    assert stream.subscribes == 1

    stream.push(_unavailable())

    assert await _until(lambda: stream.subscribes >= 2), f"{name} monitor exited for good"


async def test_monitor_resubscribes_after_a_stream_ends_cleanly(fleet):
    _, drone, _ = fleet
    stream = drone.streams["position_velocity_ned"]
    stream.push(_END)
    assert await _until(lambda: stream.subscribes >= 2)


async def test_position_flows_again_after_the_restart(fleet):
    _, drone, tel = fleet
    stream = drone.streams["position_velocity_ned"]
    stream.push(_unavailable())
    assert await _until(lambda: stream.subscribes >= 2)

    stream.push(_position(north=7.0))

    assert await _until(lambda: tel["local_x"] == 7.0)


@pytest.mark.parametrize("name", list(_SAMPLES))
async def test_drone_reads_stale_as_soon_as_any_stream_is_down(fleet, name):
    """Fresh heartbeat or not: an armed flag that can't change is not fresh."""
    manager, drone, tel = fleet
    assert manager.is_heartbeat_fresh(tel)
    # Other streams stay healthy, and last_heartbeat is still seconds old.
    drone.streams[name].refusing = True

    drone.streams[name].push(_unavailable())

    assert await _until(lambda: tel.get("telemetry_stale") is True)
    assert tel["connected"] is False
    assert tel["link_stable_since"] is None
    assert not manager.is_heartbeat_fresh(tel)
    status = await gcs_main.get_status()
    assert status["connected"] == 0
    assert status["armed"] == 0


async def test_stale_stays_set_until_the_downed_stream_really_reports_again(fleet):
    manager, drone, tel = fleet
    stream = drone.streams["position_velocity_ned"]
    stream.push(_unavailable())
    assert await _until(lambda: stream.subscribes >= 2 and tel.get("telemetry_stale") is True)

    # Resubscribed, but no data yet: being subscribed says nothing about the position.
    await asyncio.sleep(0.2)
    assert tel["telemetry_stale"] is True
    assert not manager.is_heartbeat_fresh(tel)

    # Another stream's data must not clear this one's outage.
    drone.streams["armed"].push(False)
    await asyncio.sleep(0.1)
    assert tel["telemetry_stale"] is True

    stream.push(_position(north=3.0))
    assert await _until(lambda: tel["telemetry_stale"] is False)
    assert tel["connected"] is True
    assert manager.is_heartbeat_fresh(tel)


async def test_recovery_restarts_the_link_stability_window(fleet):
    """The pre-arm heartbeat gate wants continuous link time; an outage breaks it."""
    _, drone, tel = fleet
    before = tel["link_stable_since"]
    stream = drone.streams["position_velocity_ned"]
    stream.push(_unavailable())
    assert await _until(lambda: tel["link_stable_since"] is None)
    stream.pending = []
    assert await _until(lambda: stream.subscribes >= 2)
    stream.push(_position())
    assert await _until(lambda: tel["connected"] is True)
    assert tel["link_stable_since"] > before


async def test_recovery_does_not_override_a_link_the_vehicle_says_is_down(fleet):
    _, drone, tel = fleet
    drone.streams["connection_state"].push(SimpleNamespace(is_connected=False))
    assert await _until(lambda: tel["connected"] is False)
    stream = drone.streams["position_velocity_ned"]
    stream.push(_unavailable())
    assert await _until(lambda: stream.subscribes >= 2)

    stream.push(_position())

    assert await _until(lambda: tel.get("telemetry_stale") is False)
    assert tel["connected"] is False


async def test_a_refused_resubscribe_backs_off(fleet):
    """About 0.5 + 1 + 2 + 4 s of retries, not a tight loop against a dead server."""
    _, drone, _ = fleet
    stream = drone.streams["position_velocity_ned"]
    stream.refusing = True
    stream.push(_unavailable())

    await asyncio.sleep(4.0)

    assert 2 <= stream.subscribes <= 5


async def test_a_dead_server_is_respawned_with_the_same_address(fleet):
    _, drone, _ = fleet
    drone._server_process.returncode = -signal.SIGKILL
    for stream in drone.streams.values():
        stream.refusing = True
        stream.push(_unavailable())

    assert await _until(lambda: drone._server_process is not None and drone._server_process.poll() is None)

    assert drone.connects == ["udpin://127.0.0.1:14540"], "all ten monitors failed; one respawn between them"
    assert drone.streams["position_velocity_ned"].refusing is False
    # Streams come back on the new server.
    drone.all_report()
    assert await _until(lambda: drone.streams["position_velocity_ned"].subscribes >= 2)


async def test_a_live_server_is_not_respawned_for_one_dropped_stream(fleet):
    """connect() kills and restarts the server, which would stop a live offboard stream."""
    _, drone, _ = fleet
    stream = drone.streams["position_velocity_ned"]
    stream.push(_unavailable())
    assert await _until(lambda: stream.subscribes >= 2)
    await asyncio.sleep(0.2)
    assert drone.connects == []


async def test_shutdown_stops_a_monitor_waiting_out_its_backoff(monkeypatch):
    _use_fake_system(monkeypatch)
    manager = DroneManager(ports=[14540], backend="px4")
    await manager.connect_all()
    drone = manager.drones[0]
    stream = drone.streams["position_velocity_ned"]
    stream.refusing = True
    stream.push(_unavailable())
    await asyncio.sleep(0.1)
    subscribes = stream.subscribes

    await asyncio.wait_for(manager.shutdown(), timeout=3.0)
    await asyncio.sleep(1.0)

    assert stream.subscribes == subscribes


async def test_real_server_killed_is_replaced_and_streams_come_back(monkeypatch):
    """The #171 symptom end to end, on the real binary: no GCS restart needed."""
    if not sys.platform.startswith("linux"):
        pytest.skip("real-binary check runs on Linux")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if probe.connect_ex(("127.0.0.1", 50051)) == 0:
            pytest.skip("gRPC port 50051 is taken, probably by another GCS on this machine")

    udp_port = 14978
    with HeartbeatSource(udp_port):
        manager = DroneManager(ports=[udp_port], backend="px4")
        monkeypatch.setattr(gcs_main, "drone_manager", manager)
        await manager.connect_all()
        try:
            drone = manager.drones[0]
            tel = manager.telemetry[0]
            assert await _until(lambda: tel["connected"] is True)
            first_server = drone._server_process
            pid = first_server.pid

            first_server.kill()

            assert await _until(lambda: tel.get("telemetry_stale") is True, timeout_s=3.0)
            assert await _until(
                lambda: drone._server_process is not None and drone._server_process.pid != pid, timeout_s=15.0
            )
            assert drone._server_process.poll() is None
            # The heartbeat-only vehicle sends no position, so the drone stays
            # stale here on purpose: what comes back is the link report.
            assert await _until(lambda: tel.get("telemetry_stale") is True and tel["link_up"] is True, timeout_s=15.0)
        finally:
            await manager.shutdown()
            if drone._server_process is not None:
                drone._server_process.kill()
