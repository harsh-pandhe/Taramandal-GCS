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

Authentication (issue #50). A 16-bit checksum stops a *garbled* frame, not a
*forged* one: anyone with a 915MHz transceiver can compute it, and one forged
ESTOP grounds the whole fleet at once. Every frame therefore carries an 8-byte
truncated HMAC-SHA256 tag under a pre-shared show key, plus a strictly
increasing ``frame_counter``. ``BroadcastReceiver`` is the drone-side
reference: it drops a frame whose tag fails, whose timestamp is more than
200ms from the receiver's clock, or whose counter is not above the last
accepted one. A frame that fails any check changes nothing on the receiver.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import struct
import time
from enum import Enum, IntEnum

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
#:   offset  0  uint64 utc_timestamp_usec
#:   offset  8  uint8  global_swarm_state
#:   offset  9  uint32 current_show_sequence_id
#:   offset 13  uint16 sync_checksum
#:   offset 15  uint64 frame_counter      (anti-replay, issue #50)
#:   offset 23  uint8  reserved, zero
#:   offset 24  8 bytes auth_tag          (HMAC-SHA256 of bytes 0..23, truncated)
#: struct's default alignment would insert padding between the uint8 and the
#: uint32; "<" disables that, so the layout is exactly what a drone's C parser
#: reads with the same explicit offsets.
#:
#: The first four fields sit at the offsets they had before authentication
#: existed; the counter and tag were placed in what used to be the reserved
#: tail, which is what that room was kept for.
_PAYLOAD_FORMAT = "<QBIHQ"

#: The payload is specified as 32 bytes. After the fields and the tag, one
#: byte remains reserved. It is zero-filled and covered by the tag.
PAYLOAD_SIZE = 32

#: Bytes of the HMAC-SHA256 output carried per frame. 64 bits means a forger
#: guessing blind needs ~2^63 tries on average, which a ~250kbps shared
#: channel carrying 46-byte frames cannot deliver in any show's lifetime.
AUTH_TAG_SIZE = 8
_AUTH_TAG_OFFSET = PAYLOAD_SIZE - AUTH_TAG_SIZE
_RESERVED_SIZE = _AUTH_TAG_OFFSET - struct.calcsize(_PAYLOAD_FORMAT)

#: Prefixed to every MAC input so a tag computed for this message can never
#: validate a different message type that happens to reuse the show key.
_AUTH_DOMAIN = b"TARA-BCAST\x01"

#: Shortest show key accepted. HMAC-SHA256 keys shorter than this are guessable
#: offline from a single captured frame.
SHOW_KEY_MIN_BYTES = 16

#: A frame whose timestamp is further than this from the receiver's clock is
#: dropped, which bounds how long a captured frame stays replayable.
MAX_CLOCK_SKEW_S = 0.200

#: MAVLink v2 framing around the payload: a 10-byte header, the message-id
#: portion, and a 2-byte CRC. Counted explicitly because the size budget this
#: protocol has to meet is about what occupies the air, not what we memcpy.
MAC_FRAMING_BYTES = 14

#: Total bytes on the wire for one broadcast frame.
FRAME_SIZE = PAYLOAD_SIZE + MAC_FRAMING_BYTES


class FrameStatus(str, Enum):
    """Why a received frame was accepted or dropped."""

    OK = "ok"
    MALFORMED = "malformed"
    BAD_TAG = "bad_tag"
    CLOCK_SKEW = "clock_skew"
    REPLAYED = "replayed"


def generate_show_key() -> bytes:
    """A fresh random 32-byte pre-shared key. Issue one per show."""
    return secrets.token_bytes(32)


def _check_key(key: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < SHOW_KEY_MIN_BYTES:
        raise ValueError(f"show key must be at least {SHOW_KEY_MIN_BYTES} bytes")
    return bytes(key)


def auth_tag(key: bytes, authenticated_bytes: bytes) -> bytes:
    """The truncated HMAC-SHA256 over everything in the payload before the tag."""
    return hmac.new(key, _AUTH_DOMAIN + authenticated_bytes, hashlib.sha256).digest()[:AUTH_TAG_SIZE]


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
    key: bytes,
    utc_timestamp_usec: int,
    state: SwarmState | int,
    sequence_id: int,
    frame_counter: int,
) -> bytes:
    """Pack and authenticate one downlink payload.

    Args:
        key: the pre-shared show key (at least ``SHOW_KEY_MIN_BYTES``).
        utc_timestamp_usec: UTC microseconds. This is the fleet's shared clock
            — a drone holding position in a formation cares far more that its
            neighbours agree on *when* they are than about its own absolute
            accuracy.
        state: the global ``SwarmState`` every drone should be in.
        sequence_id: which show sequence is running. Constant for the length
            of a show, so it cannot double as the anti-replay counter.
        frame_counter: strictly increasing per frame under one key; receivers
            drop any frame whose counter is not above the last one accepted.

    Returns:
        Exactly ``PAYLOAD_SIZE`` bytes, ending in the ``AUTH_TAG_SIZE``-byte tag.
    """
    key = _check_key(key)
    state = int(state)
    checksum = sync_checksum(utc_timestamp_usec, state, sequence_id)
    body = struct.pack(
        _PAYLOAD_FORMAT, utc_timestamp_usec, state, sequence_id, checksum, frame_counter
    ) + bytes(_RESERVED_SIZE)
    return body + auth_tag(key, body)


def decode_broadcast_frame(payload: bytes, key: bytes) -> dict:
    """Unpack a broadcast payload and check its authentication tag.

    This is stateless: it says whether the frame is authentic, not whether it
    is fresh. Freshness (clock skew, replay) needs the receiver's clock and
    history; see ``BroadcastReceiver``.

    Returns:
        ``{"utc_timestamp_usec", "state", "sequence_id", "sync_checksum",
        "frame_counter", "status", "valid"}``. ``status`` is
        ``FrameStatus.OK`` or ``FrameStatus.BAD_TAG``; ``valid`` is True only
        for OK. The tag is the whole verdict. It covers the checksum as well,
        so a frame whose tag verifies necessarily has a good checksum, and a
        tampered frame is reported as a tag failure even when the tamperer
        also fixed up the checksum. The caller decides what to do about a bad
        frame rather than having an exception raised at it, because the right
        response on a drone (ignore the frame, keep flying the last good one)
        is not the right response in a test.

    Raises:
        ValueError: The payload is the wrong size, or the key is too short.
    """
    key = _check_key(key)
    if len(payload) != PAYLOAD_SIZE:
        raise ValueError(f"expected {PAYLOAD_SIZE}-byte payload, got {len(payload)}")
    body, tag = payload[:_AUTH_TAG_OFFSET], payload[_AUTH_TAG_OFFSET:]
    utc, state, sequence_id, checksum, frame_counter = struct.unpack_from(_PAYLOAD_FORMAT, body)
    status = FrameStatus.OK if hmac.compare_digest(tag, auth_tag(key, body)) else FrameStatus.BAD_TAG
    return {
        "utc_timestamp_usec": utc,
        "state": state,
        "sequence_id": sequence_id,
        "sync_checksum": checksum,
        "frame_counter": frame_counter,
        "status": status,
        "valid": status is FrameStatus.OK,
    }


def downlink_bitrate_bps(hz: float = DOWNLINK_HZ) -> float:
    """Downlink load in bits/sec. Independent of fleet size, by construction."""
    return FRAME_SIZE * 8 * hz


class BroadcastManager:
    """Holds global show state and emits it, authenticated, at ``DOWNLINK_HZ``.

    The transmit side is injected rather than owned::

        sent = []
        mgr = BroadcastManager(key, transmit=lambda payload, sysid: sent.append(payload))
        mgr.set_state(SwarmState.RUN)
        mgr.tick()

    In production ``transmit`` writes to the radio; in tests it appends to a
    list. Either way the framing, cadence and authentication logic under test
    is the same code that flies.
    """

    def __init__(self, key: bytes, transmit=None, clock=None):
        """
        Args:
            key: the pre-shared show key every drone also holds.
            transmit: called with ``(payload_bytes, target_system)`` for each
                frame. Defaults to None, so a manager can be used purely as a
                state holder.
            clock: returns UTC seconds as a float. Injectable so tests can pin
                time instead of sleeping.
        """
        self._key = _check_key(key)
        self._transmit = transmit
        self._clock = clock or time.time
        self.state = SwarmState.IDLE
        self.sequence_id = 0
        self.frames_sent = 0
        self.frame_counter = 0

    def set_state(self, state: SwarmState | int) -> None:
        """Set the global state the next broadcast will carry."""
        self.state = SwarmState(int(state))

    def set_sequence(self, sequence_id: int) -> None:
        """Set the show sequence id the next broadcast will carry."""
        self.sequence_id = sequence_id

    def build_frame(self) -> bytes:
        """Encode the current state as a payload, stamped with the clock now.

        The counter is the larger of "last + 1" and the current UTC
        microseconds. The first term makes it strictly increasing within this
        process even if the clock stalls or steps back. The second makes it
        survive a GCS restart: a restarted GCS starts at "now", above anything
        its previous run sent at 10Hz, instead of at 0, which every drone
        would drop as a replay, leaving the fleet deaf to ESTOP until the key
        was rotated.
        """
        now_usec = int(self._clock() * 1_000_000)
        self.frame_counter = max(self.frame_counter + 1, now_usec)
        return encode_broadcast_frame(
            self._key, now_usec, self.state, self.sequence_id, self.frame_counter
        )

    def tick(self) -> bytes:
        """Build one frame, hand it to ``transmit`` addressed to the broadcast
        SYSID, and return it."""
        payload = self.build_frame()
        if self._transmit is not None:
            self._transmit(payload, BROADCAST_SYSID)
        self.frames_sent += 1
        return payload


class BroadcastReceiver:
    """Drone-side acceptance of downlink frames: the reference for firmware.

    A frame is accepted only if, in this order:

    1. its tag verifies under the show key (otherwise ``BAD_TAG``);
    2. its timestamp is within ``max_skew_s`` of this receiver's clock
       (otherwise ``CLOCK_SKEW``);
    3. its ``frame_counter`` is above the last accepted one (otherwise
       ``REPLAYED``).

    The tag is checked first so that nothing an unauthenticated sender
    controls, whether timestamp or counter, can influence the receiver. Only
    accepted frames update ``last_frame_counter`` and ``last_frame``. A forger
    therefore cannot push the counter so high that genuine frames start
    being rejected.

    The skew check assumes the receiver has a clock of its own that is
    independent of this link, such as GPS time. A drone that set its clock
    from these frames would be checking the frames against themselves.
    """

    def __init__(self, key: bytes, clock=None, max_skew_s: float = MAX_CLOCK_SKEW_S):
        self._key = _check_key(key)
        self._clock = clock or time.time
        self._max_skew_usec = int(max_skew_s * 1_000_000)
        self.last_frame_counter: int | None = None
        self.last_frame: dict | None = None

    def receive(self, payload: bytes) -> dict:
        """Check one frame. Returns the decoded fields plus its ``status``.

        On any status other than OK the frame has been dropped: no receiver
        state changed, and the caller should keep acting on ``last_frame``.
        """
        try:
            frame = decode_broadcast_frame(payload, self._key)
        except ValueError:
            return {"status": FrameStatus.MALFORMED, "valid": False}
        if frame["status"] is not FrameStatus.OK:
            return frame

        skew_usec = abs(int(self._clock() * 1_000_000) - frame["utc_timestamp_usec"])
        if skew_usec > self._max_skew_usec:
            return {**frame, "status": FrameStatus.CLOCK_SKEW, "valid": False}
        if self.last_frame_counter is not None and frame["frame_counter"] <= self.last_frame_counter:
            return {**frame, "status": FrameStatus.REPLAYED, "valid": False}

        self.last_frame_counter = frame["frame_counter"]
        self.last_frame = frame
        return frame
