"""The virtual serial bridge that stands in for a UART when there is no autopilot on the bench (issue #67)."""

import asyncio
import importlib.util
import os
import signal
import socket
import sys
import time
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location("serial_bridge", Path(__file__).resolve().parents[3] / "tools" / "serial_bridge.py")
sb = importlib.util.module_from_spec(_SPEC)
sys.modules["serial_bridge"] = sb
_SPEC.loader.exec_module(sb)


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _started(tmp_path, baud=None, buffer_s=0.25):
    bridge = sb.Bridge(str(tmp_path / "vserial0"), "127.0.0.1", _free_udp_port(), baud, buffer_s)
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: sb._Proto(bridge), local_addr=("127.0.0.1", bridge.udp_port))
    bridge.plug()
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.1", 0))
    peer.setblocking(False)
    return bridge, peer


async def _read_until(fd, n, timeout=2.0):
    got = b""
    end = time.monotonic() + timeout
    while len(got) < n and time.monotonic() < end:
        try:
            got += os.read(fd, 4096)
        except (BlockingIOError, InterruptedError):
            await asyncio.sleep(0.01)
    return got


@pytest.mark.asyncio
async def test_bytes_flow_both_ways_between_the_device_and_the_udp_peer(tmp_path):
    bridge, peer = await _started(tmp_path)
    try:
        dev = os.open(bridge.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        peer.sendto(b"\xfd\x09hello-from-px4", ("127.0.0.1", bridge.udp_port))  # also teaches the bridge its peer
        assert await _read_until(dev, 16) == b"\xfd\x09hello-from-px4"
        os.write(dev, b"\xfd\x01to-px4")
        loop = asyncio.get_running_loop()
        data, addr = await asyncio.wait_for(loop.sock_recvfrom(peer, 4096), 2.0)
        assert data == b"\xfd\x01to-px4"
        os.close(dev)
    finally:
        bridge.unplug()
        peer.close()


@pytest.mark.asyncio
async def test_the_device_is_raw_nothing_is_echoed_or_translated(tmp_path):
    bridge, peer = await _started(tmp_path)
    try:
        dev = os.open(bridge.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        peer.sendto(b"\x00", ("127.0.0.1", bridge.udp_port))
        await asyncio.sleep(0.05)
        os.write(dev, b"a\r\nb\x03\x04\x1a")
        loop = asyncio.get_running_loop()
        data, _ = await asyncio.wait_for(loop.sock_recvfrom(peer, 4096), 2.0)
        assert data == b"a\r\nb\x03\x04\x1a"
        assert await _read_until(dev, 1, timeout=0.2) == b"\x00"  # only what the peer sent, no echo of our write
        os.close(dev)
    finally:
        bridge.unplug()
        peer.close()


@pytest.mark.asyncio
async def test_a_baud_limit_drops_what_the_line_cannot_carry_and_counts_it(tmp_path):
    bridge, peer = await _started(tmp_path, baud=9600, buffer_s=0.25)  # 960 bytes a second, a 240 byte backlog
    try:
        dev = os.open(bridge.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        for _ in range(30):  # 30 x 100 bytes in a burst: far more than 0.25 s of a 9600 baud line
            peer.sendto(b"x" * 100, ("127.0.0.1", bridge.udp_port))
        await asyncio.sleep(0.3)
        carried, dropped = bridge.stats["to_serial_bytes"], bridge.stats["dropped_to_serial_bytes"]
        assert carried + dropped == 3000
        assert 100 <= carried <= 500, carried  # about a quarter second of the line
        assert dropped >= 2500
        os.close(dev)
    finally:
        bridge.unplug()
        peer.close()


@pytest.mark.asyncio
async def test_without_a_limit_nothing_is_dropped(tmp_path):
    bridge, peer = await _started(tmp_path)
    try:
        dev = os.open(bridge.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        for _ in range(30):
            peer.sendto(b"x" * 100, ("127.0.0.1", bridge.udp_port))
        await asyncio.sleep(0.2)
        assert bridge.stats["dropped_to_serial_bytes"] == 0
        assert bridge.stats["to_serial_bytes"] == 3000
        os.close(dev)
    finally:
        bridge.unplug()
        peer.close()


@pytest.mark.asyncio
async def test_pulling_the_cable_removes_the_path_and_plugging_it_back_restores_it(tmp_path):
    bridge, peer = await _started(tmp_path)
    try:
        assert os.path.exists(bridge.link)
        dev = os.open(bridge.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        bridge.unplug()
        assert not os.path.exists(bridge.link)  # the device is gone, as on an unplugged USB cable
        peer.sendto(b"lost", ("127.0.0.1", bridge.udp_port))
        await asyncio.sleep(0.05)
        assert bridge.stats["pulls"] == 1 and bridge.stats["dropped_to_serial_bytes"] >= 4
        bridge.plug()
        assert os.path.exists(bridge.link)
        dev2 = os.open(bridge.link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        peer.sendto(b"again", ("127.0.0.1", bridge.udp_port))
        assert await _read_until(dev2, 5) == b"again"
        for fd in (dev, dev2):
            with __import__("contextlib").suppress(OSError):
                os.close(fd)
    finally:
        bridge.unplug()
        peer.close()


def test_the_signals_pull_and_replug_a_running_bridge(tmp_path):
    import subprocess
    link = tmp_path / "vs"
    proc = subprocess.Popen([sys.executable, str(Path(sb.__file__)), "--link", str(link), "--udp-listen", str(_free_udp_port())],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert "virtual serial device" in proc.stdout.readline()
        assert link.exists() or link.is_symlink()
        os.kill(proc.pid, signal.SIGUSR1)
        end = time.time() + 2
        while time.time() < end and os.path.lexists(link):
            time.sleep(0.02)
        assert not os.path.lexists(link)
        os.kill(proc.pid, signal.SIGUSR2)
        end = time.time() + 2
        while time.time() < end and not os.path.lexists(link):
            time.sleep(0.02)
        assert os.path.lexists(link)
    finally:
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=5)
    assert "'pulls': 1" in out
