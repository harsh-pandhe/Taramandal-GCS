"""The fleet-discovery listener and watchdog (issue #58), over real UDP.

The last group is the issue's three acceptance criteria, run with real separate processes on
ordinary ephemeral UDP ports and the issue's own numbers (discovered within 1 s, STALE within 2 s,
the same slot back after a re-power).
"""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tara_gcs import fleet_discovery as fd
from tara_gcs import fleet_registry as fr
from tara_gcs import main as gcs_main
from tara_gcs.fleet_registry import FleetRegistry, State

KEY = bytes(range(32))
TOOL = Path(__file__).resolve().parents[3] / "tools" / "fake_drone_announce.py"


async def started(**registry_kw):
    registry = FleetRegistry(key=KEY, **registry_kw)
    service = fd.FleetDiscovery(registry, host="127.0.0.1", port=0, sweep_period_s=0.05)
    await service.start()
    return service


def send(service, payload):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(payload, ("127.0.0.1", service.port))
    s.close()


async def until(predicate, timeout=3.0, step=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


@pytest.mark.asyncio
async def test_an_announcement_over_udp_registers_the_drone():
    service = await started()
    try:
        send(service, fr.encode_announcement(KEY, 0xABC))
        assert await until(lambda: service.registry.online_ids() == [0])
        assert [e["kind"] for e in service.events] == ["joined"]
        status = service.status()
        assert status["enabled"] and status["online"] == 1 and status["drones"][0]["hardware_uuid"] == f"{0xABC:016x}"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_junk_and_forged_packets_are_counted_and_do_not_stop_the_listener():
    service = await started()
    try:
        for junk in (b"", b"x", os.urandom(30), fr.encode_announcement(bytes(range(1, 33)), 5), b"\xff" * 2000):
            send(service, junk)
        send(service, fr.encode_announcement(KEY, 0xABC))  # a good one after all of them
        assert await until(lambda: service.registry.online_ids() == [0])
        rejected = service.registry.rejected
        assert rejected["bad_tag"] >= 1 and rejected["malformed"] >= 1
        assert list(service.registry.entries) == [0xABC]
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_the_watchdog_marks_a_quiet_drone_stale_and_records_it():
    service = await started(stale_after_s=0.3)
    try:
        send(service, fr.encode_announcement(KEY, 0xABC))
        assert await until(lambda: service.registry.online_ids() == [0])
        assert await until(lambda: service.registry.entries[0xABC].state is State.STALE, timeout=2.0)
        assert [e["kind"] for e in service.events] == ["joined", "stale"]
        # ...and a later announcement brings it back, once
        send(service, fr.encode_announcement(KEY, 0xABC))
        assert await until(lambda: service.registry.entries[0xABC].state is State.ONLINE)
        assert [e["kind"] for e in service.events][-1] == "recovered"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_a_failing_registry_does_not_kill_the_receive_path_or_the_watchdog():
    service = await started(stale_after_s=0.3)
    try:
        real_observe, real_sweep = service.registry.observe, service.registry.sweep
        calls = {"n": 0}

        def broken_observe(data):
            calls["n"] += 1
            raise RuntimeError("boom")

        service.registry.observe = broken_observe
        service.registry.sweep = lambda: (_ for _ in ()).throw(RuntimeError("sweep boom"))
        send(service, b"anything")
        assert await until(lambda: calls["n"] == 1)
        await asyncio.sleep(0.2)  # several failing sweeps
        service.registry.observe, service.registry.sweep = real_observe, real_sweep
        send(service, fr.encode_announcement(KEY, 0xABC))
        assert await until(lambda: service.registry.online_ids() == [0])
        assert await until(lambda: service.registry.entries[0xABC].state is State.STALE, timeout=2.0)
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_stop_closes_the_socket_and_is_safe_twice():
    service = await started()
    port = service.port
    await service.stop()
    await service.stop()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", port))  # the port is free again
    s.close()


@pytest.mark.asyncio
async def test_the_event_list_keeps_only_the_most_recent_events():
    service = await started()
    try:
        for i in range(fd.RECENT_EVENTS + 20):
            service._record(fr.Transition("stale", i + 1, 0, 0.0))
        assert len(service.events) == fd.RECENT_EVENTS and service.events[-1]["hardware_uuid"] == f"{fd.RECENT_EVENTS + 20:016x}"
    finally:
        await service.stop()


# ------------------------------------------------------------ settings from the environment

def test_it_is_off_unless_asked_for():
    assert fd.build_from_env({}) is None
    assert fd.build_from_env({"GCS_DISCOVERY": "0", "GCS_DISCOVERY_KEY": KEY.hex()}) is None
    assert fd.build_from_env({"GCS_DISCOVERY": "true", "GCS_DISCOVERY_KEY": KEY.hex()}) is None  # only "1"


def test_it_refuses_to_run_without_a_key(caplog):
    assert fd.build_from_env({"GCS_DISCOVERY": "1"}) is None
    assert "unauthenticated" in caplog.text


@pytest.mark.parametrize("key", ["zz", "ab" * 15, ""])
def test_it_refuses_a_bad_key(key):
    env = {"GCS_DISCOVERY": "1", "GCS_DISCOVERY_KEY": key}
    assert fd.build_from_env(env) is None


def test_it_reads_the_key_from_a_file_and_builds_the_service(tmp_path):
    keyfile = tmp_path / "discovery.key"
    keyfile.write_text(KEY.hex() + "\n")
    service = fd.build_from_env({
        "GCS_DISCOVERY": "1", "GCS_DISCOVERY_KEY_FILE": str(keyfile), "GCS_DISCOVERY_PORT": "15999",
        "GCS_DISCOVERY_HOST": "127.0.0.1", "GCS_DISCOVERY_STALE_S": "2.5",
    }, store_dir=str(tmp_path))
    assert service.port == 15999 and service.host == "127.0.0.1"
    assert service.registry.key == KEY and service.registry.stale_after_s == 2.5
    assert str(service.registry.store_path) == str(tmp_path / "fleet_registry.json")


def test_a_missing_key_file_is_an_error_not_an_open_door(tmp_path):
    assert fd.build_from_env({"GCS_DISCOVERY": "1", "GCS_DISCOVERY_KEY_FILE": str(tmp_path / "nope")}) is None


def test_the_default_bind_is_loopback():
    service = fd.build_from_env({"GCS_DISCOVERY": "1", "GCS_DISCOVERY_KEY": KEY.hex()})
    assert service.host == "127.0.0.1" and service.port == fd.DEFAULT_PORT


# ------------------------------------------ the issue's acceptance criteria, with real processes

def _drone(service, uuid, key=KEY):
    proc = subprocess.Popen(
        [sys.executable, str(TOOL), "--key-hex", key.hex(), "--uuid", hex(uuid), "--port", str(service.port), "--rate", "2"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return proc


def _first_sent(proc):
    line = proc.stdout.readline()
    assert line.startswith("FIRST_SENT"), line + proc.stderr.read()
    return float(line.split()[1])


@pytest.mark.asyncio
async def test_acceptance_discovery_stale_warning_and_slot_reclaim_with_real_processes():
    service = await started()  # the real defaults: stale after 1.5 s, swept every 50 ms here
    procs = []
    try:
        # 1. Starting a new drone process on an arbitrary UDP port is discovered within 1 second.
        drone = _drone(service, 0xA1B2C3D4E5F60718)
        procs.append(drone)
        sent_at = _first_sent(drone)
        assert await until(lambda: service.registry.online_ids() == [0], timeout=2.0)
        discovered_after = time.time() - sent_at
        assert discovered_after < 1.0, f"discovered after {discovered_after:.2f} s"
        original = service.registry.entries[0xA1B2C3D4E5F60718].drone_id

        # a second drone, so the slot the first one holds is not simply "the only one"
        other = _drone(service, 0x1111111111111111)
        procs.append(other)
        _first_sent(other)
        assert await until(lambda: service.registry.online_ids() == [0, 1], timeout=2.0)

        # 2. Killing a drone triggers a STALE warning within 2 seconds (SIGKILL: no goodbye).
        killed_at = time.time()
        os.kill(drone.pid, signal.SIGKILL)
        drone.wait(timeout=5)
        assert await until(lambda: service.registry.entries[0xA1B2C3D4E5F60718].state is State.STALE, timeout=3.0)
        stale_after = time.time() - killed_at
        assert stale_after < 2.0, f"STALE after {stale_after:.2f} s"
        assert service.registry.online_ids() == [1]  # the other drone is untouched
        assert service.events[-1]["kind"] == "stale" and service.events[-1]["drone_id"] == original

        # 3. A re-powered drone (a new process, a new source port) reclaims its original slot by hardware id.
        again = _drone(service, 0xA1B2C3D4E5F60718)
        procs.append(again)
        _first_sent(again)
        assert await until(lambda: service.registry.online_ids() == [0, 1], timeout=2.0)
        assert service.registry.entries[0xA1B2C3D4E5F60718].drone_id == original
        assert service.events[-1]["kind"] == "recovered"
        assert len(service.registry.entries) == 2  # it did not get a third slot
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=5)
            p.stdout.close()
            p.stderr.close()
        await service.stop()


@pytest.mark.asyncio
async def test_a_process_with_the_wrong_key_is_never_discovered():
    service = await started()
    proc = _drone(service, 0xBAD, key=bytes(range(1, 33)))
    try:
        _first_sent(proc)
        assert await until(lambda: service.registry.rejected["bad_tag"] >= 2, timeout=3.0)
        assert service.registry.entries == {} and service.registry.online_ids() == []
    finally:
        proc.kill()
        proc.wait(timeout=5)
        proc.stdout.close()
        proc.stderr.close()
        await service.stop()


# ------------------------------------------------------------------ through the GCS app

def _enable(monkeypatch, tmp_path, **extra):
    monkeypatch.setenv("GCS_DISCOVERY", "1")
    monkeypatch.setenv("GCS_DISCOVERY_KEY", KEY.hex())
    monkeypatch.setenv("GCS_DISCOVERY_PORT", "0")
    monkeypatch.setenv("GCS_DISCOVERY_STORE", str(tmp_path / "registry.json"))
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def test_the_api_says_discovery_is_off_by_default(monkeypatch):
    monkeypatch.delenv("GCS_DISCOVERY", raising=False)
    with TestClient(gcs_main.app) as client:
        assert client.get("/api/fleet/registry").json() == {"enabled": False}
        assert gcs_main.fleet_discovery_service is None


def test_a_discovered_drone_shows_up_in_the_api_and_leaves_the_fleet_manager_alone(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    with TestClient(gcs_main.app) as client:
        service = gcs_main.fleet_discovery_service
        assert service is not None and service.port > 0
        telemetry_before = set(gcs_main.drone_manager.telemetry)
        send(service, fr.encode_announcement(KEY, 0xFEED))
        deadline = time.monotonic() + 3
        body = client.get("/api/fleet/registry").json()
        while body.get("online") != 1 and time.monotonic() < deadline:
            time.sleep(0.02)
            body = client.get("/api/fleet/registry").json()
        assert body["enabled"] and body["online"] == 1 and body["stale_after_s"] == 1.5
        assert body["drones"][0]["hardware_uuid"] == f"{0xFEED:016x}" and body["drones"][0]["state"] == "ONLINE"
        assert body["events"][0]["kind"] == "joined"
        # Discovery only keeps a registry: it created no connection, no telemetry entry, no state change.
        assert set(gcs_main.drone_manager.telemetry) == telemetry_before
    assert gcs_main.fleet_discovery_service is not None  # kept after shutdown; the socket is closed (next test)


def test_the_listener_is_closed_when_the_gcs_shuts_down(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    with TestClient(gcs_main.app):
        port = gcs_main.fleet_discovery_service.port
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", port))
    s.close()


def test_the_gcs_starts_without_discovery_when_its_key_is_missing(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    monkeypatch.delenv("GCS_DISCOVERY_KEY")
    with TestClient(gcs_main.app) as client:
        assert client.get("/api/fleet/registry").json() == {"enabled": False}
        assert client.get("/api/status").status_code == 200  # the GCS itself is up


def test_the_gcs_starts_without_discovery_when_its_port_is_taken(monkeypatch, tmp_path):
    taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    taken.bind(("127.0.0.1", 0))
    try:
        _enable(monkeypatch, tmp_path, GCS_DISCOVERY_PORT=str(taken.getsockname()[1]))
        with TestClient(gcs_main.app) as client:
            assert client.get("/api/fleet/registry").json() == {"enabled": False}
            assert client.get("/api/status").status_code == 200
    finally:
        taken.close()


@pytest.mark.real_auth
def test_the_registry_route_needs_a_login(monkeypatch):
    monkeypatch.delenv("GCS_DISCOVERY", raising=False)
    with TestClient(gcs_main.app) as client:
        assert client.get("/api/fleet/registry").status_code == 401
