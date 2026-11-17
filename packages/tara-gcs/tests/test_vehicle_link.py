"""Vehicle addresses: accepted only in a form this GCS connects to, never guessed (issue #67)."""

import pytest

from tara_gcs.vehicle_link import BAUD_RATES, parse_vehicle_addresses, udp_address, validate_address


def test_the_default_udp_address_is_what_it_always_was():
    assert udp_address(14540) == "udpin://127.0.0.1:14540"
    assert validate_address(udp_address(14540)) == "udpin://127.0.0.1:14540"


@pytest.mark.parametrize(
    "address",
    ["udpin://127.0.0.1:14540", "udpout://192.168.1.20:14550", "serial:///dev/ttyACM0:921600",
     "serial:///dev/ttyUSB0:57600", "serial:///tmp/tara-vserial0:115200", "serial:///dev/serial/by-id/usb-3DR_FMU-if00:921600"],
)
def test_accepted_addresses_are_returned_unchanged(address):
    assert validate_address(address) == address


@pytest.mark.parametrize(
    "address, why",
    [
        ("", "empty"), (" udpin://127.0.0.1:14540", "whitespace"), ("udpin://127.0.0.1:14540 ", "whitespace"),
        ("udpin://127.0.0.1:0", "port"), ("udpin://127.0.0.1:70000", "port"), ("udpin://:14540", "not one this GCS"),
        ("tcpout://127.0.0.1:5760", "not one this GCS"), ("serial://dev/ttyACM0:921600", "not one this GCS"),
        ("serial:///dev/ttyACM0", "not one this GCS"),  # no baud: none is assumed
        ("serial:///dev/ttyACM0:12345", "baud rate"), ("serial:///dev/ttyACM0:921600 ", "whitespace"),
        ("serial:///dev/../etc/shadow:57600", "'..'"), ("serial:///dev/tty ACM0:57600", "not one this GCS"),
        ("serial:///dev/ttyACM0:57600;rm", "not one this GCS"), (None, "empty"), (14540, "empty"),
    ],
)
def test_anything_else_is_refused_with_a_reason(address, why):
    with pytest.raises(ValueError) as exc:
        validate_address(address)
    assert why in str(exc.value)


def test_every_listed_baud_rate_is_accepted_and_only_those():
    for baud in BAUD_RATES:
        assert validate_address(f"serial:///dev/ttyACM0:{baud}")
    with pytest.raises(ValueError):
        validate_address("serial:///dev/ttyACM0:115201")


def test_the_environment_value_maps_drone_indices_to_addresses():
    out = parse_vehicle_addresses(" 0=serial:///dev/ttyACM0:921600 , 2=udpin://127.0.0.1:14542 ", fleet_size=3)
    assert out == {0: "serial:///dev/ttyACM0:921600", 2: "udpin://127.0.0.1:14542"}


@pytest.mark.parametrize("text", [None, "", "   "])
def test_no_value_is_no_override(text):
    assert parse_vehicle_addresses(text, fleet_size=3) == {}


@pytest.mark.parametrize(
    "text, why",
    [
        ("serial:///dev/ttyACM0:921600", "must look like"),     # no index: which drone?
        ("x=serial:///dev/ttyACM0:921600", "must look like"),
        ("3=serial:///dev/ttyACM0:921600", "outside the fleet"),
        ("0=serial:///dev/ttyACM0:921600,0=udpin://127.0.0.1:14540", "twice"),
        ("0=serial:///dev/ttyACM0:1", "baud rate"),
        ("0=serial:///dev/ttyACM0:921600,1=bogus", "not one this GCS"),
    ],
)
def test_a_bad_entry_refuses_the_whole_map(text, why):
    with pytest.raises(ValueError) as exc:
        parse_vehicle_addresses(text, fleet_size=3)
    assert why in str(exc.value)


# ------------------------------------------------------------------ DroneManager


import asyncio  # noqa: E402
import time  # noqa: E402

from tara_gcs import drone_manager as dm  # noqa: E402


def test_a_manager_without_addresses_connects_every_drone_on_udp_as_before():
    m = dm.DroneManager(ports=[14540, 14541], backend="px4")
    assert m._vehicle_address(0, 14540) == "udpin://127.0.0.1:14540"
    assert m._vehicle_address(1, 14541) == "udpin://127.0.0.1:14541"


def test_a_configured_address_replaces_only_its_drone():
    m = dm.DroneManager(ports=[14540, 14541], backend="px4", vehicle_addresses={0: "serial:///dev/ttyACM0:921600"})
    assert m._vehicle_address(0, 14540) == "serial:///dev/ttyACM0:921600"
    assert m._vehicle_address(1, 14541) == "udpin://127.0.0.1:14541"


@pytest.mark.parametrize(
    "backend, addresses, why",
    [("px4", {0: "bogus"}, "not one this GCS"), ("px4", {5: "serial:///dev/ttyACM0:921600"}, "outside the fleet"),
     ("sim", {0: "serial:///dev/ttyACM0:921600"}, "backend='px4' only")],
)
def test_the_manager_refuses_a_bad_map_when_it_is_built(backend, addresses, why):
    with pytest.raises(ValueError) as exc:
        dm.DroneManager(ports=[14540, 14541], backend=backend, vehicle_addresses=addresses)
    assert why in str(exc.value)


def test_the_configured_address_is_what_mavsdk_is_asked_to_connect_to(monkeypatch):
    seen = {}

    class FakeSystem:
        def __init__(self, port):
            self.port = port

        async def connect(self, system_address):
            seen[self.port] = system_address

    monkeypatch.setattr(dm, "GcsBoundSystem", FakeSystem)
    m = dm.DroneManager(ports=[14540, 14541], backend="px4", vehicle_addresses={1: "serial:///tmp/tara-vserial1:115200"})

    async def go():
        for d, p in enumerate(m.ports):
            await m._connect_drone(d, p)

    try:
        asyncio.run(go())
    except Exception:  # noqa: BLE001 - the fake has no telemetry plugins; only the connect call matters here
        pass
    assert seen[50051] == "udpin://127.0.0.1:14540"
    assert seen[50052] == "serial:///tmp/tara-vserial1:115200"


# ------------------------------------------------------------------ the serial link watchdog (cable pulled and replugged)


class _FakeDrone:
    def __init__(self):
        self.connects: list[tuple[float, str]] = []
        self.fail = False

    async def connect(self, system_address):
        import time as _t
        self.connects.append((_t.monotonic(), system_address))
        if self.fail:
            raise RuntimeError("could not open the device")


@pytest.fixture
def watched(tmp_path, monkeypatch):
    device = tmp_path / "ttyVTARA0"
    address = f"serial://{device}:115200"
    m = dm.DroneManager(ports=[14540], backend="px4", vehicle_addresses={0: address})
    fake = _FakeDrone()
    m.drones[0] = fake
    # The real failure of a pulled cable: the connection flag stays set (the server's streams stay open); only heartbeats stop.
    m.telemetry[0] = {"connected": True, "last_heartbeat": time.time() - 10.0, "port": 14540}
    monkeypatch.setattr(dm.DroneManager, "SERIAL_WATCHDOG_POLL_S", 0.02)
    monkeypatch.setattr(dm.DroneManager, "SERIAL_RELINK_AFTER_S", 0.1)
    monkeypatch.setattr(dm.DroneManager, "SERIAL_RELINK_STALE_S", 0.5)
    monkeypatch.setattr(dm.DroneManager, "SERIAL_RELINK_BACKOFF_S", (0.3, 0.6))
    return m, fake, device, address


def _link_up(m):
    m.telemetry[0]["connected"] = True
    m.telemetry[0]["last_heartbeat"] = time.time()


def _link_down(m):
    m.telemetry[0]["last_heartbeat"] = time.time() - 10.0  # flag still set, heartbeats stale


async def _run_for(manager, seconds):
    task = asyncio.create_task(manager._serial_link_watchdog(0))
    try:
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_while_the_cable_is_out_nothing_is_reconnected_and_on_replug_it_happens_once(watched):
    m, fake, device, address = watched
    task = asyncio.create_task(m._serial_link_watchdog(0))
    try:
        await asyncio.sleep(0.4)  # link down, device absent
        assert fake.connects == []
        device.write_text("")  # the cable goes back in
        await asyncio.sleep(0.25)
        assert [a for _, a in fake.connects] == [address]
        _link_up(m)  # and the link comes up
        await asyncio.sleep(0.2)
        assert len(fake.connects) == 1
    finally:
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_a_healthy_link_is_never_touched(watched):
    m, fake, device, _ = watched
    device.write_text("")
    _link_up(m)
    await _run_for(m, 0.8)
    assert fake.connects == []


@pytest.mark.asyncio
async def test_a_link_down_with_the_device_always_present_waits_for_the_longer_threshold(watched):
    m, fake, device, _ = watched
    device.write_text("")
    task = asyncio.create_task(m._serial_link_watchdog(0))
    try:
        await asyncio.sleep(0.35)  # past SERIAL_RELINK_AFTER_S but not past SERIAL_RELINK_STALE_S, never seen absent
        assert fake.connects == []
        await asyncio.sleep(0.4)
        assert len(fake.connects) == 1
    finally:
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_a_vehicle_that_stays_silent_is_retried_on_a_growing_backoff_not_in_a_loop(watched):
    m, fake, device, _ = watched
    await _noop()
    task = asyncio.create_task(m._serial_link_watchdog(0))
    try:
        await asyncio.sleep(0.15)
        device.write_text("")  # replug; the vehicle never answers
        await asyncio.sleep(1.6)
    finally:
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task
    times = [t for t, _ in fake.connects]
    assert 2 <= len(times) <= 4, len(times)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps[0] >= 0.28  # at least the first backoff (0.3 s) between the first two attempts
    if len(gaps) > 1:
        assert gaps[1] >= 0.58  # then the next one (0.6 s)


async def _noop():
    return None


@pytest.mark.asyncio
async def test_a_failed_reconnect_does_not_stop_the_watchdog(watched):
    m, fake, device, _ = watched
    fake.fail = True
    device.write_text("")
    task = asyncio.create_task(m._serial_link_watchdog(0))
    try:
        await asyncio.sleep(1.4)
        assert len(fake.connects) >= 2  # it kept trying
        fake.fail = False
        _link_up(m)
        await asyncio.sleep(0.1)
        assert not task.done()
    finally:
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_after_a_recovery_the_next_outage_is_handled_afresh(watched):
    m, fake, device, _ = watched
    task = asyncio.create_task(m._serial_link_watchdog(0))
    try:
        await asyncio.sleep(0.15)
        device.write_text("")
        await asyncio.sleep(0.25)
        assert len(fake.connects) == 1
        _link_up(m)  # recovered
        await asyncio.sleep(0.1)
        device.unlink()  # a second pull
        _link_down(m)
        await asyncio.sleep(0.3)
        assert len(fake.connects) == 1  # absent: nothing to reconnect
        device.write_text("")
        await asyncio.sleep(0.3)  # no leftover backoff from the first outage
        assert len(fake.connects) == 2
    finally:
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task


def test_only_a_serial_address_has_a_device():
    from tara_gcs.vehicle_link import serial_device

    assert serial_device("serial:///dev/ttyACM0:921600") == "/dev/ttyACM0"
    assert serial_device("udpin://127.0.0.1:14540") is None
    assert serial_device(None) is None


# ------------------------------------------------------------------ the GCS startup


import subprocess  # noqa: E402
import sys  # noqa: E402


def _start_gcs_module(addresses: str):
    import os

    env = {**os.environ, "GCS_VEHICLE_ADDRESSES": addresses, "NUM_DRONES": "2", "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    return subprocess.run([sys.executable, "-W", "ignore", "-c", "import tara_gcs.main as m; print('ADDR', m.drone_manager.vehicle_addresses)"],
                          env=env, capture_output=True, text=True, timeout=60)


def test_a_bad_address_in_the_environment_stops_the_gcs_from_starting_and_says_why():
    done = _start_gcs_module("0=bogus")
    assert done.returncode != 0
    assert "not one this GCS" in done.stderr


def test_an_address_for_a_drone_outside_the_fleet_stops_the_gcs_from_starting():
    done = _start_gcs_module("5=serial:///dev/ttyACM0:921600")
    assert done.returncode != 0 and "outside the fleet" in done.stderr


def test_a_good_address_in_the_environment_reaches_the_manager():
    done = _start_gcs_module("1=serial:///dev/ttyACM0:921600")
    assert done.returncode == 0, done.stderr
    assert "ADDR {1: 'serial:///dev/ttyACM0:921600'}" in done.stdout
