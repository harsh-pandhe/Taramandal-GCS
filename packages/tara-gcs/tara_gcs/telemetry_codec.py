"""The dual-channel telemetry protocol `/ws/telemetry` streams to the dashboard.

The single-channel design this replaces (`websocket.send_json(drone_manager.telemetry)`
at 5Hz) serializes every field for every drone every tick. At 100 drones that's
several hundred KB/s of JSON; at 1000 it stops being viable at all. Fields split
into two channels instead, each streamed at the rate it actually needs:

  * **Fast channel** (binary, ``FAST_HZ``): fleet-frame and local-frame
    position, heading, armed, connected — the fields a live 3D view and the
    per-drone coordinate readout both need to update smoothly. One flat
    ``Float32Array``, no JSON parsing, no per-field key names sent over the
    wire — 17 floats x 4 bytes = 68 bytes per drone (6.8KB/frame at 100 drones,
    20Hz -> ~136KB/s, versus several hundred KB/s of JSON for the same data).
    At the 1000-drone ceiling that is 68KB/frame, ~1.4MB/s — still an order of
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
``range(len(ports))``, so today the two coincide).

Every fast frame starts with a ``HEADER_FLOATS``-float header::

    [FRAME_MAGIC, FRAME_VERSION, FLOATS_PER_DRONE]

so a receiver can tell a frame it understands from one it does not. Without it a
GCS and a dashboard built from different versions of ``FAST_FIELDS`` read each
other's records at the wrong stride and show plausible-looking wrong positions,
silently (found when the LED fields grew the record from 13 to 17 floats, #266).
The receiver refuses a frame whose header differs from its own and says why.
Bump ``FRAME_VERSION`` for any layout change that keeps ``FLOATS_PER_DRONE``
(a reordered or re-scaled field); a changed width is caught by the third word
on its own. The client reads the drone count as
``(byteLength - HEADER_BYTES) / BYTES_PER_DRONE``.
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
    "led_r", "led_g", "led_b", "led_valid",
)
FLOATS_PER_DRONE = len(FAST_FIELDS)
BYTES_PER_DRONE = FLOATS_PER_DRONE * 4

#: Frame header, in float32 words: magic, layout version, floats per drone. Kept
#: in sync by hand with packages/tara-web/src/telemetry.js. The magic is an
#: integer well away from any value a position or flag word takes at word 0 of a
#: pre-header frame (fleet_x of drone 0), so an old GCS is rejected, not misread.
FRAME_MAGIC = 7243.0
FRAME_VERSION = 1.0
HEADER_FLOATS = 3
HEADER_BYTES = HEADER_FLOATS * 4

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
        A little-endian ``Float32Array``-compatible byte string: the
        ``HEADER_FLOATS`` header, then the drones in sorted-ID order,
        ``FLOATS_PER_DRONE`` floats each. An empty fleet is the header alone.
    """
    values: list[float] = [FRAME_MAGIC, FRAME_VERSION, float(FLOATS_PER_DRONE)]
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
        # LED colour (0-255) the show commands at this instant, from the colours
        # baked into the trajectory; ``led_valid`` says whether the show has any
        # (a show without light data leaves the dashboard on its own colours).
        values.append(float(tel.get("led_r", 0.0)))
        values.append(float(tel.get("led_g", 0.0)))
        values.append(float(tel.get("led_b", 0.0)))
        values.append(1.0 if tel.get("led_valid") else 0.0)
    return struct.pack(f"<{len(values)}f", *values)


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
