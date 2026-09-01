"""The 0.5Hz slotted TDMA uplink — every drone gets its own moment to talk.

The uplink is the half of the fleet link that genuinely scales with N: each
drone has its *own* battery, waypoint index and position error, so there is no
broadcast trick available the way there is on the downlink. What there is
instead is a schedule.

Left to themselves, 500 CSMA/CA radios sharing one channel do not take turns;
they transmit, collide, back off, and collide again, and the throughput curve
past saturation bends downward rather than flattening. Slotting removes the
contention rather than managing it: the 2-second epoch is divided into N
disjoint micro-slots, drone i owns slot i and transmits in no other, so two
drones never key up at once and the MAC layer never has a collision to resolve.
At N=500 each slot is 4ms wide — comfortably more than the ~0.96ms a 30-byte
frame occupies at 250kbps, which is what leaves room for clock jitter and
radio turnaround (see ``slot_utilization``).

The epoch is 2 seconds because 0.5Hz is genuinely enough for this payload.
Battery, waypoint index and RTK status move slowly, and position *error* is a
health signal rather than a control input — nothing in the loop closes through
it. Live position for the audience-facing views comes from the show's own
choreography, which the GCS already knows; it does not need to be told.

Slot assignment is deliberately arithmetic (``drone_id * epoch / N``) and not
a table: a drone can compute its own slot from its ID and the broadcast
timestamp alone, needing no assignment handshake and no per-drone state on the
GCS to go stale. That is also what makes the schedule survive a drone
rebooting mid-show — it rejoins on exactly the slot it left.
"""

from __future__ import annotations

import struct

#: One full round of the uplink schedule. Every drone transmits exactly once
#: per epoch, so this is the reciprocal of the 0.5Hz per-drone report rate.
EPOCH_SECONDS = 2.0
UPLINK_HZ = 1.0 / EPOCH_SECONDS

#: Wire layout of one compressed status payload, little-endian:
#:   uint16 drone_id, uint16 vbat_mv, uint16 current_wp_idx, uint8 rtk_status,
#:   uint8 fault_flags, int16 x_err_cm, int16 y_err_cm, int16 z_err_cm
#: Errors are centimetres in int16 rather than float metres: it halves the
#: field and still resolves ±327m, far past the point where a position error
#: has stopped being a telemetry reading and started being an incident.
_STATUS_FORMAT = "<HHHBBhhh"

#: The eight fields occupy 14 bytes; the payload is specified as 16, leaving
#: two reserved and zero-filled for later additions.
STATUS_PAYLOAD_SIZE = 16
_STATUS_RESERVED = STATUS_PAYLOAD_SIZE - struct.calcsize(_STATUS_FORMAT)

#: Same MAVLink v2 framing overhead the downlink pays, per uplink frame.
MAC_FRAMING_BYTES = 14
STATUS_FRAME_SIZE = STATUS_PAYLOAD_SIZE + MAC_FRAMING_BYTES

#: The 915MHz link's practical capacity. The budget this module exists to
#: respect is a fraction of this, not all of it.
RADIO_CAPACITY_BPS = 250_000

STATUS_FIELDS = (
    "drone_id",
    "vbat_mv",
    "current_wp_idx",
    "rtk_status",
    "fault_flags",
    "x_err_cm",
    "y_err_cm",
    "z_err_cm",
)


def encode_status_payload(
    drone_id: int,
    vbat_mv: int,
    current_wp_idx: int,
    rtk_status: int,
    fault_flags: int,
    x_err_cm: int,
    y_err_cm: int,
    z_err_cm: int,
) -> bytes:
    """Pack one drone's compressed status into ``STATUS_PAYLOAD_SIZE`` bytes."""
    payload = struct.pack(
        _STATUS_FORMAT,
        drone_id,
        vbat_mv,
        current_wp_idx,
        rtk_status,
        fault_flags,
        x_err_cm,
        y_err_cm,
        z_err_cm,
    )
    return payload + bytes(_STATUS_RESERVED)


def decode_status_payload(payload: bytes) -> dict:
    """Unpack a compressed status payload into a field dict.

    Returns:
        A dict keyed by ``STATUS_FIELDS``.
    """
    if len(payload) != STATUS_PAYLOAD_SIZE:
        raise ValueError(
            f"expected {STATUS_PAYLOAD_SIZE}-byte payload, got {len(payload)}"
        )
    size = struct.calcsize(_STATUS_FORMAT)
    values = struct.unpack(_STATUS_FORMAT, payload[:size])
    return dict(zip(STATUS_FIELDS, values))


class TDMASlot:
    """One drone's reserved window within the epoch: ``[start, end)``.

    Half-open on purpose — slot i ending at exactly the microsecond slot i+1
    begins is what makes "disjoint" and "gapless" true at the same time.
    """

    __slots__ = ("drone_id", "start_s", "end_s")

    def __init__(self, drone_id: int, start_s: float, end_s: float):
        self.drone_id = drone_id
        self.start_s = start_s
        self.end_s = end_s

    @property
    def width_s(self) -> float:
        return self.end_s - self.start_s

    def contains(self, epoch_offset_s: float) -> bool:
        """True if a time offset within the epoch falls in this slot."""
        return self.start_s <= epoch_offset_s < self.end_s

    def overlaps(self, other: "TDMASlot") -> bool:
        """True if two slots share any instant — the thing that must never
        happen between two different drones."""
        return self.start_s < other.end_s and other.start_s < self.end_s

    def __repr__(self) -> str:
        return (
            f"TDMASlot(drone_id={self.drone_id}, "
            f"start_s={self.start_s:.6f}, end_s={self.end_s:.6f})"
        )


class TDMAScheduler:
    """Divides the epoch into N disjoint micro-slots, one per drone.

        sched = TDMAScheduler(fleet_size=500)
        sched.slot_width_s          # 0.004
        sched.slot_for(7).start_s   # 0.028

    Holds no per-drone mutable state: every answer is derived from
    ``fleet_size`` and the drone's own ID, which is what lets a drone compute
    its own slot independently and arrive at the same one the GCS expects.
    """

    def __init__(self, fleet_size: int, epoch_s: float = EPOCH_SECONDS):
        if fleet_size <= 0:
            raise ValueError(f"fleet_size must be positive, got {fleet_size}")
        if epoch_s <= 0:
            raise ValueError(f"epoch_s must be positive, got {epoch_s}")
        self.fleet_size = fleet_size
        self.epoch_s = epoch_s

    @property
    def slot_width_s(self) -> float:
        """Seconds of airtime each drone owns per epoch."""
        return self.epoch_s / self.fleet_size

    def slot_for(self, drone_id: int) -> TDMASlot:
        """The slot belonging to ``drone_id``.

        Computed rather than looked up, so this is O(1) and needs no table
        distributed to the fleet.
        """
        if not 0 <= drone_id < self.fleet_size:
            raise ValueError(
                f"drone_id {drone_id} outside fleet of {self.fleet_size}"
            )
        # Both endpoints are derived the same way, as i * epoch / N, rather than
        # as start + width. The two are not equivalent in floating point: at
        # N=500, `9 * 0.004 + 0.004` lands 7e-18 above `10 * 0.004`, so slot 9
        # would end fractionally *after* slot 10 began — 22 of the 499
        # boundaries overlap that way. Sharing the numerator makes every
        # boundary bit-identical and the slots exactly disjoint.
        return TDMASlot(
            drone_id,
            drone_id * self.epoch_s / self.fleet_size,
            (drone_id + 1) * self.epoch_s / self.fleet_size,
        )

    def all_slots(self) -> list[TDMASlot]:
        """Every slot in the epoch, in transmission order."""
        return [self.slot_for(i) for i in range(self.fleet_size)]

    def drone_for_offset(self, epoch_offset_s: float) -> int:
        """Which drone owns the instant ``epoch_offset_s`` into the epoch.

        The inverse of ``slot_for``, and the check a GCS uses to tell whether
        a frame arrived in its sender's own slot or somewhere it should not
        have.
        """
        if not 0 <= epoch_offset_s < self.epoch_s:
            raise ValueError(
                f"offset {epoch_offset_s} outside epoch of {self.epoch_s}s"
            )
        idx = int(epoch_offset_s / self.slot_width_s)
        return min(idx, self.fleet_size - 1)

    def transmission_time_s(self, capacity_bps: int = RADIO_CAPACITY_BPS) -> float:
        """How long one status frame actually occupies the air."""
        return STATUS_FRAME_SIZE * 8 / capacity_bps

    def slot_utilization(self, capacity_bps: int = RADIO_CAPACITY_BPS) -> float:
        """Fraction of a slot the frame itself uses.

        The headroom left over is not waste — it absorbs clock drift between
        drones and radio turnaround time. A utilization near 1.0 would mean a
        drone whose clock is a millisecond fast transmits into its neighbour's
        slot, which is the exact failure slotting exists to prevent.
        """
        return self.transmission_time_s(capacity_bps) / self.slot_width_s

    def uplink_bitrate_bps(self) -> float:
        """Aggregate uplink load for the whole fleet, in bits/sec.

        Every drone sends one frame per epoch, so this is N frames spread over
        ``epoch_s`` — the one term in the budget that grows with fleet size.
        """
        return self.fleet_size * STATUS_FRAME_SIZE * 8 / self.epoch_s


def total_bitrate_bps(fleet_size: int, epoch_s: float = EPOCH_SECONDS) -> float:
    """Combined downlink + uplink channel load for a fleet, in bits/sec."""
    from .broadcast_manager import downlink_bitrate_bps

    return (
        downlink_bitrate_bps()
        + TDMAScheduler(fleet_size, epoch_s).uplink_bitrate_bps()
    )


def channel_utilization(
    fleet_size: int,
    epoch_s: float = EPOCH_SECONDS,
    capacity_bps: int = RADIO_CAPACITY_BPS,
) -> float:
    """Fraction of the radio's capacity the whole protocol consumes.

    The number the asymmetric design exists to hold down: ~0.25 at 500 drones,
    against the >1 (i.e. collapse) that 10Hz unicast telemetry would demand.
    """
    return total_bitrate_bps(fleet_size, epoch_s) / capacity_bps
