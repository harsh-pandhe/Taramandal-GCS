"""A MAVLink heartbeat source for tests that run the real mavsdk_server binary.

mavsdk_server opens its gRPC port only after discovering a vehicle, so a test
of the server's own lifetime needs just enough of a vehicle for that. Run from
the test process, so that it outlives a GCS the test kills: a vehicle going
silent makes mavsdk_server log, and logging to a dead parent's closed pipe is
SIGPIPE, which would end the server by accident and hide what is being tested.
"""

import socket
import struct
import threading

_HEARTBEAT_CRC_EXTRA = 50


def _x25(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        tmp = byte ^ (crc & 0xFF)
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def _heartbeat_frame(seq: int) -> bytes:
    # custom_mode, MAV_TYPE_QUADROTOR, MAV_AUTOPILOT_PX4, base_mode, STANDBY, version
    payload = struct.pack("<IBBBBB", 0, 2, 12, 0, 3, 3)
    body = bytes([len(payload), seq & 0xFF, 1, 1, 0]) + payload
    crc = _x25(body + bytes([_HEARTBEAT_CRC_EXTRA]))
    return b"\xfe" + body + struct.pack("<H", crc)


class HeartbeatSource:
    """Sends a PX4-looking heartbeat to a UDP port at 5 Hz until stopped."""

    def __init__(self, port: int):
        self._port = port
        self._stop = threading.Event()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        seq = 0
        while not self._stop.wait(0.2):
            self._socket.sendto(_heartbeat_frame(seq), ("127.0.0.1", self._port))
            seq += 1

    def __enter__(self) -> "HeartbeatSource":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._socket.close()
