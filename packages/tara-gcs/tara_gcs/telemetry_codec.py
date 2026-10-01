"""The dual-channel telemetry protocol `/ws/telemetry` streams to the dashboard.

The single-channel design this replaces (`websocket.send_json(drone_manager.telemetry)`
at 5Hz) serializes every field for every drone every tick. At 100 drones that's
several hundred KB/s of JSON; at 1000 it stops being viable at all. Fields split
into two channels instead, each streamed at the rate it actually needs:

  * **Fast channel** (binary, ``FAST_HZ``): fleet-frame and local-frame
    position, heading, armed, connected — the fields a live 3D view and the
    per-drone coordinate readout both need to update smoothly. One flat
    ``Float32Array``, no JSON parsing, no per-field key names sent over the
    wire — 13 floats x 4 bytes = 52 bytes per drone (5.2KB/frame at 100 drones,
    20Hz -> ~104KB/s, versus several hundred KB/s of JSON for the same data).
    At the 1000-drone ceiling that is 52KB/frame, ~1MB/s — still an order of
    magnitude under the equivalent JSON.
  * **Slow channel** (JSON text, ``SLOW_HZ``): battery, GPS, flight mode,
    comm latency, home position, and the static UDP port — everything that
    changes at human-perceptible speed (or never, for the port), where JSON's
    readability and the browser's native parser are more valuable than the
    last few hundred bytes.

Both channels share one WebSocket connection: binary frames arrive as
``ArrayBuffer`` and text frames as ``string`` in the browser, so the client
tells them apart by ``event.data`` type without needing a wrapper protocol.

Drones are keyed by their sorted integer ID; the fast frame's Nth
``BYTES_PER_DRONE``-byte record is drone ID N (the *sorted position*, not the ID
value — but `DroneManager.telemetry` keys are always a contiguous
``range(len(ports))``, so today the two coincide). There is no header — the
client determines drone count as ``buffer.byteLength / BYTES_PER_DRONE``.
"""

from __future__ import annotations

import struct

#: Order matters — this is the exact layout of one fast-frame record. Kept in
#: sync by hand with packages/tara-web/src/telemetry.js's decoder.
FAST_FIELDS = (
    "fleet_x", "fleet_y", "fleet_z",
    "local_x", "local_y", "local_z",
    "heading", "armed", "connected",
    "planned_x", "planned_y", "planned_z", "planned_valid",
)
FLOATS_PER_DRONE = len(FAST_FIELDS)
BYTES_PER_DRONE = FLOATS_PER_DRONE * 4

SLOW_FIELDS = (
    "flight_mode",
    "battery_percent",
    "battery_voltage",
    "gps_lock",
    "satellites",
    "gps_fix_type",
    "comm_latency_ms",
    "home_lat",
    "home_lon",
    "home_alt",
    "port",
    # None, or {"type": "rtl"|"land"|"estop", "at": epoch seconds} for a drone
    # aborted out of the show on its own (issue #59). Slow channel, not fast:
    # it changes once per abort, and the fast frame's binary layout is
    # hand-synced with the dashboard's decoder.
    "aborted",
)

FAST_HZ = 20.0
SLOW_HZ = 1.0


def encode_fast_frame(telemetry: dict[int, dict]) -> bytes:
    """Pack position/heading/armed/connected for every drone into one binary frame.

    Args:
        telemetry: ``DroneManager.telemetry`` — drone id -> status dict.

    Returns:
        A little-endian ``Float32Array``-compatible byte string, drones in
        sorted-ID order, ``FLOATS_PER_DRONE`` floats each.
    """
    values: list[float] = []
    for drone_id in sorted(telemetry.keys()):
        tel = telemetry[drone_id]
        values.append(float(tel.get("fleet_x", 0.0)))
        values.append(float(tel.get("fleet_y", 0.0)))
        values.append(float(tel.get("fleet_z", 0.0)))
        values.append(float(tel.get("local_x", 0.0)))
        values.append(float(tel.get("local_y", 0.0)))
        values.append(float(tel.get("local_z", 0.0)))
        values.append(float(tel.get("heading", 0.0)))
        values.append(1.0 if tel.get("armed") else 0.0)
        values.append(1.0 if tel.get("connected") else 0.0)
        # Planned (commanded) fleet-frame position, written by the trajectory
        # playback loop. ``planned_valid`` is the flag the client needs to tell
        # "deviation is genuinely zero" apart from "no trajectory is running, so
        # there is nothing to deviate from" — without it a parked fleet would
        # render as uniformly nominal-green rather than as unmonitored.
        values.append(float(tel.get("planned_x", 0.0)))
        values.append(float(tel.get("planned_y", 0.0)))
        values.append(float(tel.get("planned_z", 0.0)))
        values.append(1.0 if tel.get("planned_valid") else 0.0)
    return struct.pack(f"<{len(values)}f", *values) if values else b""


def encode_slow_frame(telemetry: dict[int, dict], fleet_state: dict | None = None) -> dict:
    """Build the slow-channel JSON payload: everything not in the fast frame.

    Args:
        telemetry: ``DroneManager.telemetry``.
        fleet_state: ``FleetStateMachine.snapshot()``, sent as the frame's
            ``"fleet_state"`` key when given (issue #52).

    Returns:
        ``{"type": "slow", "drones": {drone_id_str: {field: value, ...}}}``.
        The ``"type"`` key lets the client tell this apart from any other JSON
        message on the same socket without inspecting shape.
    """
    frame = {
        "type": "slow",
        "drones": {
            str(drone_id): {field: telemetry[drone_id].get(field) for field in SLOW_FIELDS}
            for drone_id in sorted(telemetry.keys())
        },
    }
    if fleet_state is not None:
        frame["fleet_state"] = fleet_state
    return frame
