#!/usr/bin/env python3
"""A virtual serial device for the GCS, bridged to a UDP MAVLink link (issue #67).

There is no external autopilot on this machine, so the serial transport is exercised against PX4 SITL through a pseudo-terminal:
this tool creates one, links it at a path the GCS can open as ``serial:///<path>:<baud>``, and forwards bytes both ways between
it and a UDP endpoint (PX4 SITL's onboard link, ``--udp-listen 14540``: the peer is the first sender, which PX4 sets to its local
port). It is a stand-in for the wire, **not a real UART**: no electrical layer, no real device driver, no USB.

What it models, on purpose:
* ``--baud N``: the line carries N/10 bytes a second (8N1). Bytes arriving faster than that queue for at most ``--buffer-s`` seconds
  and are then dropped, whole datagrams at a time, the way a saturated serial telemetry port loses messages. Drops are counted.
* a **cable pull**: SIGUSR1 closes the device and removes its path (the GCS' read fails, as when a USB cable is unplugged); SIGUSR2
  creates a fresh device at the same path (plugged back in). ``--pull-at S --pull-for S`` does the same on a timer.

Usage: python tools/serial_bridge.py --link /tmp/tara-vserial0 --udp-listen 14540 --baud 921600
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
import time
import tty


class Bridge:
    def __init__(self, link: str, udp_host: str, udp_port: int, baud: int | None, buffer_s: float):
        self.link, self.udp_host, self.udp_port = link, udp_host, udp_port
        self.bytes_per_s = None if not baud else baud / 10.0
        self.buffer_s = buffer_s
        self.master = self.slave = -1
        self.peer: tuple[str, int] | None = None
        self.transport: asyncio.DatagramTransport | None = None
        self.stats = {"to_serial_bytes": 0, "to_udp_bytes": 0, "dropped_to_serial_bytes": 0, "dropped_to_udp_bytes": 0, "pulls": 0}
        self._serial_free_at = 0.0  # when the line is next free in each direction (a token bucket of one)
        self._udp_free_at = 0.0
        self._plugged = False

    # ---- the device
    def plug(self) -> None:
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)  # no echo, no newline translation: the wire carries bytes
        tmp = self.link + ".new"
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        os.symlink(os.ttyname(self.slave), tmp)
        os.replace(tmp, self.link)
        os.set_blocking(self.master, False)
        asyncio.get_running_loop().add_reader(self.master, self._on_serial_readable)
        self._plugged = True

    def unplug(self, pull: bool = True) -> None:
        """Remove the device. ``pull`` counts it as a cable pull (False at shutdown)."""
        if not self._plugged:
            return
        loop = asyncio.get_running_loop()
        loop.remove_reader(self.master)
        for fd in (self.master, self.slave):
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.link)
        self._plugged = False
        if pull:
            self.stats["pulls"] += 1

    # ---- serial -> UDP
    def _on_serial_readable(self) -> None:
        try:
            data = os.read(self.master, 4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            return
        if not data or self.peer is None or self.transport is None:
            return
        if self._admit("udp", len(data)):
            self.transport.sendto(data, self.peer)
            self.stats["to_udp_bytes"] += len(data)
        else:
            self.stats["dropped_to_udp_bytes"] += len(data)

    # ---- UDP -> serial
    def on_datagram(self, data: bytes, addr) -> None:
        if self.peer is None:
            self.peer = addr
        if not self._plugged:
            self.stats["dropped_to_serial_bytes"] += len(data)  # nothing on the other end of the cable
            return
        if not self._admit("serial", len(data)):
            self.stats["dropped_to_serial_bytes"] += len(data)
            return
        try:
            os.write(self.master, data)
            self.stats["to_serial_bytes"] += len(data)
        except OSError:
            self.stats["dropped_to_serial_bytes"] += len(data)

    def _admit(self, direction: str, nbytes: int) -> bool:
        """The line takes nbytes in nbytes/rate seconds; refuse a write that would wait longer than the buffer."""
        if self.bytes_per_s is None:
            return True
        now = time.monotonic()
        attr = "_serial_free_at" if direction == "serial" else "_udp_free_at"
        free_at = max(getattr(self, attr), now)
        if free_at - now > self.buffer_s:
            return False
        setattr(self, attr, free_at + nbytes / self.bytes_per_s)
        return True


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, bridge: Bridge):
        self.bridge = bridge

    def connection_made(self, transport):
        self.bridge.transport = transport

    def datagram_received(self, data, addr):
        self.bridge.on_datagram(data, addr)


async def run(args) -> int:
    bridge = Bridge(args.link, args.udp_host, args.udp_listen, args.baud, args.buffer_s)
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: _Proto(bridge), local_addr=(args.udp_host, args.udp_listen))
    bridge.plug()
    stop = asyncio.Event()
    loop.add_signal_handler(signal.SIGUSR1, bridge.unplug)
    loop.add_signal_handler(signal.SIGUSR2, lambda: bridge.plug() if not bridge._plugged else None)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    if args.pull_at is not None:
        loop.call_later(args.pull_at, bridge.unplug)
        loop.call_later(args.pull_at + args.pull_for, lambda: bridge.plug() if not bridge._plugged else None)
    print(f"serial bridge: {args.link} <-> udp {args.udp_host}:{args.udp_listen}"
          f"{f', {args.baud} baud' if args.baud else ', unlimited'} (a virtual serial device, not a real UART)", flush=True)
    await stop.wait()
    bridge.unplug(pull=False)
    print("serial bridge stats:", bridge.stats, flush=True)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--link", required=True, help="path to create for the device, e.g. /tmp/tara-vserial0")
    p.add_argument("--udp-host", default="127.0.0.1")
    p.add_argument("--udp-listen", type=int, required=True, help="UDP port to listen on (PX4 SITL onboard link: 14540)")
    p.add_argument("--baud", type=int, default=None, help="limit the line to baud/10 bytes a second (default: unlimited)")
    p.add_argument("--buffer-s", type=float, default=0.25, help="seconds of backlog the line holds before dropping")
    p.add_argument("--pull-at", type=float, default=None, help="seconds after start to simulate pulling the cable")
    p.add_argument("--pull-for", type=float, default=5.0, help="seconds the cable stays out")
    args = p.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
