"""The 10Hz global broadcast downlink — one frame for the whole fleet.

Unicast telemetry does not survive contact with a real 915MHz radio. Streaming
10Hz state to 500 drones individually asks for 500 addressed frames every
100ms; against the ~250kbps the link actually carries, offered load is roughly
8Mbps and CSMA/CA collapses — every node backs off, retries, and collides
again, so goodput falls toward zero exactly when the show needs it most.

The asymmetry is the fix. What the fleet needs *from* the GCS at 10Hz is
identical for every drone: what time is it, what state is the show in, which
sequence are we running. That is one broadcast frame, not N unicast frames —
``target_system = 0`` in MAVLink means "everyone", so a single transmission
serves 500 drones as cheaply as it serves one, and the downlink cost stops
scaling with fleet size entirely (see ``downlink_bitrate_bps``: 3.68kbps at
any N). What the GCS needs *from* each drone is per-drone and therefore
genuinely N-sized, so it goes on the slotted uplink at 0.5Hz instead — see
``tdma_scheduler``.

This module is the frame codec and the transmit cadence. It deliberately does
not own a serial port: ``BroadcastManager`` takes a ``transmit`` callable, so
the same engine drives a real radio in production, and a list-appending stub
in tests, without a mock of a serial library in sight.

Note this is the *fleet RF* protocol, and a different concern from
``telemetry_codec`` — that one is the GCS-to-browser WebSocket transport,
where bandwidth is essentially free and the design problem is JSON overhead
rather than a shared collision domain.
"""

from __future__ import annotations

import struct
import time
from enum import IntEnum

#: MAVLink's broadcast address. A frame addressed here is processed by every
#: drone on the channel, which is the entire point of the downlink: cost is
#: per-frame, not per-drone.
BROADCAST_SYSID = 0

#: Downlink cadence. Fast enough that a drone's clock and show-state view stay
#: tight between frames, slow enough to leave the channel almost entirely free
#: for the uplink.
DOWNLINK_HZ = 10.0


class SwarmState(IntEnum):
    """Global show state, broadcast to the whole fleet as one byte.

    Every drone acts on the *same* value from the *same* frame, which is what
    makes a fleet-wide ESTOP or HOLD land simultaneously rather than rippling
    across 500 separate acknowledged commands.
    """

    IDLE = 0
    ARMED = 1
    RUN = 2
    HOLD = 3
    RTL = 4
    ESTOP = 5


#: Wire layout of the broadcast payload, little-endian:
#:   uint64 utc_timestamp_usec, uint8 global_swarm_state,
#:   uint32 current_show_sequence_id, uint16 sync_checksum
#: struct's default alignment would insert padding between the uint8 and the
#: uint32; "<" disables that, so the layout is exactly what a drone's C parser
#: reads with the same explicit offsets.
_PAYLOAD_FORMAT = "<QBIH"

#: The four fields occupy 15 bytes. The payload is specified as 32 so the
#: remainder is reserved — deliberate room to add fields later without
#: renumbering a message every drone in the fleet already parses. Zero-filled.
PAYLOAD_SIZE = 32
_RESERVED_SIZE = PAYLOAD_SIZE - struct.calcsize(_PAYLOAD_FORMAT)

#: MAVLink v2 framing around the payload: a 10-byte header, the message-id
#: portion, and a 2-byte CRC. Counted explicitly because the size budget this
#: protocol has to meet is about what occupies the air, not what we memcpy.
MAC_FRAMING_BYTES = 14

#: Total bytes on the wire for one broadcast frame.
FRAME_SIZE = PAYLOAD_SIZE + MAC_FRAMING_BYTES


def sync_checksum(utc_timestamp_usec: int, state: int, sequence_id: int) -> int:
    """A 16-bit integrity check over the three meaningful downlink fields.

    This is the drone's guard against acting on a corrupted frame — a garbled
    ESTOP that decodes as RUN is the failure this exists to prevent. Folding
    the 32-bit sum's high half back in means a bit flipped in the *upper* half
    of the timestamp still changes the result, which a bare 16-bit truncation
    would silently drop.
    """
    total = (utc_timestamp_usec & 0xFFFFFFFF) + (utc_timestamp_usec >> 32)
    total += (state & 0xFF) + (sequence_id & 0xFFFFFFFF)
    total = (total & 0xFFFF) + (total >> 16)
    return (total + (total >> 16)) & 0xFFFF


def encode_broadcast_frame(
    utc_timestamp_usec: int,
    state: SwarmState | int,
    sequence_id: int,
) -> bytes:
    """Pack one downlink payload: time, global state, sequence, checksum.

    Args:
        utc_timestamp_usec: UTC microseconds. This is the fleet's shared clock
            — a drone holding position in a formation cares far more that its
            neighbours agree on *when* they are than about its own absolute
            accuracy.
        state: the global ``SwarmState`` every drone should be in.
        sequence_id: which show sequence is running.

    Returns:
        Exactly ``PAYLOAD_SIZE`` bytes, zero-padded past the four fields.
    """
    state = int(state)
    checksum = sync_checksum(utc_timestamp_usec, state, sequence_id)
    payload = struct.pack(
        _PAYLOAD_FORMAT, utc_timestamp_usec, state, sequence_id, checksum
    )
    return payload + bytes(_RESERVED_SIZE)


def decode_broadcast_frame(payload: bytes) -> dict:
    """Unpack a broadcast payload and verify its checksum.

    Returns:
        ``{"utc_timestamp_usec", "state", "sequence_id", "sync_checksum",
        "valid"}``. ``valid`` is False when the checksum disagrees; the caller
        decides what to do about it rather than having an exception raised at
        it, because the right response on a drone (ignore the frame, keep
        flying the last good one) is not the right response in a test.
    """
    if len(payload) != PAYLOAD_SIZE:
        raise ValueError(f"expected {PAYLOAD_SIZE}-byte payload, got {len(payload)}")
    size = struct.calcsize(_PAYLOAD_FORMAT)
    utc, state, sequence_id, checksum = struct.unpack(_PAYLOAD_FORMAT, payload[:size])
    return {
        "utc_timestamp_usec": utc,
        "state": state,
        "sequence_id": sequence_id,
        "sync_checksum": checksum,
        "valid": checksum == sync_checksum(utc, state, sequence_id),
    }


def downlink_bitrate_bps(hz: float = DOWNLINK_HZ) -> float:
    """Downlink load in bits/sec. Independent of fleet size, by construction."""
    return FRAME_SIZE * 8 * hz


class BroadcastManager:
    """Holds global show state and emits it at ``DOWNLINK_HZ``.

    The transmit side is injected rather than owned::

        sent = []
        mgr = BroadcastManager(transmit=lambda payload, sysid: sent.append(payload))
        mgr.set_state(SwarmState.RUN)
        mgr.tick()

    In production ``transmit`` writes to the radio; in tests it appends to a
    list. Either way the framing, cadence and checksum logic under test is the
    same code that flies.
    """

    def __init__(self, transmit=None, clock=None):
        """
        Args:
            transmit: called with ``(payload_bytes, target_system)`` for each
                frame. Defaults to None, so a manager can be used purely as a
                state holder.
            clock: returns UTC seconds as a float. Injectable so tests can pin
                time instead of sleeping.
        """
        self._transmit = transmit
        self._clock = clock or time.time
        self.state = SwarmState.IDLE
        self.sequence_id = 0
        self.frames_sent = 0

    def set_state(self, state: SwarmState | int) -> None:
        """Set the global state the next broadcast will carry."""
        self.state = SwarmState(int(state))

    def set_sequence(self, sequence_id: int) -> None:
        """Set the show sequence id the next broadcast will carry."""
        self.sequence_id = sequence_id

    def build_frame(self) -> bytes:
        """Encode the current state as a payload, stamped with the clock now."""
        return encode_broadcast_frame(
            int(self._clock() * 1_000_000), self.state, self.sequence_id
        )

    def tick(self) -> bytes:
        """Build one frame, hand it to ``transmit`` addressed to the broadcast
        SYSID, and return it."""
        payload = self.build_frame()
        if self._transmit is not None:
            self._transmit(payload, BROADCAST_SYSID)
        self.frames_sent += 1
        return payload
