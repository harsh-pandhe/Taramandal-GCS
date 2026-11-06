"""A configurable RF channel model for `tara_sim` (issue #62).

`FleetState` already has a connectivity dropout knob
(`SimConfig.dropout_prob_per_tick`): a flat, distance-independent probability
that a drone's `connected` flag *flips* each tick — a memoryless telegraph
process, good enough to exercise failsafe code paths but not shaped like a
real radio link, whose loss grows with distance and comes in bursts (fading,
interference) rather than independent coin-flips.

`RFChannelModel` is a separate, opt-in mechanism `FleetState` composes
instead of (never alongside — see `FleetState.__init__`'s validation) the
flat dropout, built from two loss terms and an optional third:

1. **Distance-based packet-error-rate (PER).** A drone's base loss
   probability grows linearly with its horizontal distance from its own
   launch pad — the closest available proxy in this simulator for "distance
   from a ground-based GCS antenna near the show's centre": `FleetState`
   has no separate concept of a GCS antenna position, and a light show's
   radius is small enough that a linear approximation over a real
   log-distance path-loss curve is indistinguishable in practice. This is a
   deliberate simplification, documented rather than silently assumed; a
   venue where the GCS sits far from the launch area would need a real
   antenna-position parameter this module does not yet have.
2. **Gilbert-Elliott burst loss.** A two-state (GOOD/BAD) Markov chain per
   drone, independent across drones. PER is `burst_loss_multiplier` times
   higher in the BAD state. Default transition probabilities give short,
   infrequent bursts rather than a drone being permanently degraded.

3. **TDMA slot jitter (issue #207, opt-in).** The slot geometry is the GCS
   uplink's (`tara_gcs.tdma_scheduler`): a 2 s epoch split into one slot
   per drone, drone ``i`` in slot ``i``, and a 30-byte status frame that is
   on air for 0.96 ms at 250 kbps. Each physics tick here stands for one
   epoch, the same one-packet-per-drone-per-tick abstraction as the loss
   terms above. With ``tdma_jitter_ms > 0``, every drone's send time is off
   by a fresh normal draw (standard deviation ``tdma_jitter_ms``) each
   tick. The frame is centred in its slot, so the guard on each side is
   ``(slot width - frame time) / 2``: 1.52 ms at 500 drones, 0.52 ms at
   1000. An offset beyond the guard runs into a neighbouring slot, late
   into the next drone's and early into the previous one's, and both
   packets of that collision are lost. The last slot's late neighbour is
   the first (the next epoch's first slot, folded into the same tick).
   Only adjacent slots are considered: an offset larger than a whole slot
   plus the guard (5.52 ms at 500 drones) would really reach two slots
   away, and this model still counts it as one collision with the neighbour.

The rest of tara_sim's radio has no slot structure (`SimFleet.broadcast`
is one shared channel), so this shapes which packets are lost and nothing
else. The PX4 backend never sees any of it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


#: The GCS uplink's slot geometry (`tara_gcs.tdma_scheduler`): one 2 s epoch
#: shared by the whole fleet, and a 30-byte frame (16 payload + 14 framing) at
#: 250 kbps. Copied, not imported: tara_sim does not depend on tara_gcs. A test
#: checks the two stay equal.
UPLINK_EPOCH_MS = 2000.0
UPLINK_FRAME_MS = 30 * 8 / 250_000 * 1000.0  # 0.96 ms


def uplink_guard_ms(n: int) -> float:
    """Guard on each side of a centred frame in an ``n``-drone uplink slot, ms.

    Zero or negative once the slot is no wider than the frame (from 2084
    drones): there is then no timing margin at all.
    """
    return (UPLINK_EPOCH_MS / n - UPLINK_FRAME_MS) / 2.0


@dataclass
class RFChannelConfig:
    """Tunable parameters for :class:`RFChannelModel`.

    Attributes:
        packet_loss_base: Packet-error rate at zero distance from each
            drone's own pad, in the channel's GOOD state. ``[0, 1]``. ``0.0``
            (the default) disables the whole model — see `enabled` below.
        packet_loss_per_100m: Additional PER per 100m of horizontal distance
            from the drone's pad, added to `packet_loss_base` before
            clipping to ``[0, 1]`` — the distance-based attenuation term.
        burst_loss_multiplier: PER while in the Gilbert-Elliott BAD state is
            the (already-clipped) base PER times this, clipped again to
            ``[0, 1]``. Must be ``>= 1.0`` — a "burst" that is not worse
            than the good state is a contradiction in terms.
        p_good_to_bad: Per-tick probability of entering the BAD state from
            GOOD. Small by default — bursts are the exception, not the norm.
        p_bad_to_good: Per-tick probability of recovering from BAD to GOOD.
            The mean BAD-state duration is ``1 / p_bad_to_good`` ticks.
        tdma_jitter_ms: Standard deviation of each drone's TDMA send-time
            error, milliseconds, drawn fresh every frame (issue #207).
            Finite and ``>= 0``. ``0.0`` (the default) turns slot jitter off.
        tdma_guard_ms: Guard interval on each side of a slot, milliseconds:
            a send-time error beyond it runs into the neighbouring slot and
            both packets are lost. ``None`` (the default) derives it from the
            fleet size and the GCS uplink's slot geometry (see
            `uplink_guard_ms`); a number overrides that and must be finite
            and ``> 0``.
    """

    packet_loss_base: float = 0.0
    packet_loss_per_100m: float = 0.0
    burst_loss_multiplier: float = 4.0
    p_good_to_bad: float = 0.01
    p_bad_to_good: float = 0.3
    tdma_jitter_ms: float = 0.0
    tdma_guard_ms: float | None = None

    def __post_init__(self) -> None:
        for name in ("packet_loss_base", "packet_loss_per_100m", "p_good_to_bad", "p_bad_to_good"):
            value = getattr(self, name)
            if value < 0.0:
                raise ValueError(f"{name} must be >= 0, got {value}")
        if self.burst_loss_multiplier < 1.0:
            raise ValueError(f"burst_loss_multiplier must be >= 1.0, got {self.burst_loss_multiplier}")
        # `not (x >= 0)` also catches NaN, which compares false with everything.
        if not (math.isfinite(self.tdma_jitter_ms) and self.tdma_jitter_ms >= 0.0):
            raise ValueError(f"tdma_jitter_ms must be finite and >= 0, got {self.tdma_jitter_ms}")
        if self.tdma_guard_ms is not None and not (math.isfinite(self.tdma_guard_ms) and self.tdma_guard_ms > 0.0):
            raise ValueError(f"tdma_guard_ms must be None or finite and > 0, got {self.tdma_guard_ms}")

    @property
    def enabled(self) -> bool:
        """Whether this config would ever drop a packet.

        `FleetState` checks this to decide whether to build a model at all —
        an all-zero config is a no-op by construction, not just by never
        rolling a loss, so a caller that constructs `RFChannelConfig()` and
        never touches it gets exactly pre-#62 behaviour.
        """
        return self.packet_loss_base > 0.0 or self.packet_loss_per_100m > 0.0 or self.tdma_jitter_ms > 0.0


class RFChannelModel:
    """Per-drone Gilbert-Elliott packet-loss state, stepped once per physics tick.

    Args:
        n: Number of drones.
        config: Loss-rate tuning.
        rng: Shared random generator — pass `FleetState`'s own `_rng` so a
            seeded `SimConfig.rng_seed` makes the whole tick (physics noise,
            wind, dropout *and* this model) reproducible from one seed,
            matching issue #62's "runs with identical seed produce identical
            packet drop sequences" acceptance criterion.
    """

    def __init__(self, n: int, config: RFChannelConfig, rng: np.random.Generator):
        self.config = config
        self._rng = rng
        #: Guard on each side of a slot for this fleet size, ms (issue #207).
        self.tdma_guard_ms = config.tdma_guard_ms if config.tdma_guard_ms is not None else uplink_guard_ms(n)
        if config.tdma_jitter_ms > 0.0 and self.tdma_guard_ms <= 0.0:
            raise ValueError(
                f"no TDMA guard time left at {n} drones: a {UPLINK_EPOCH_MS / n:.3f} ms slot cannot hold a "
                f"{UPLINK_FRAME_MS:.2f} ms frame, so slot jitter cannot be modelled (set tdma_guard_ms to override)"
            )
        self.bad_state = np.zeros(n, dtype=bool)
        #: Per-drone running counts, for the GCS-facing "live packet drop
        #: rate" metric (see `drop_rate`) — not reset by anything short of
        #: recreating this model.
        self.packets_dropped = np.zeros(n, dtype=np.int64)
        self.packets_total = np.zeros(n, dtype=np.int64)
        #: Of `packets_dropped`, how many were lost to a TDMA slot collision
        #: (issue #207). A packet lost both ways counts once in each.
        self.packets_collided = np.zeros(n, dtype=np.int64)

    def step(self, distance_m: np.ndarray) -> np.ndarray:
        """Advance every drone's channel by one tick.

        Args:
            distance_m: Each drone's current horizontal distance from its
                own pad, metres, shape ``(n,)``.

        Returns:
            Boolean array, shape ``(n,)``: whether that drone's packet was
            dropped this tick.
        """
        cfg = self.config
        n = self.bad_state.shape[0]

        to_bad = ~self.bad_state & (self._rng.random(n) < cfg.p_good_to_bad)
        to_good = self.bad_state & (self._rng.random(n) < cfg.p_bad_to_good)
        self.bad_state[to_bad] = True
        self.bad_state[to_good] = False

        base_per = np.clip(cfg.packet_loss_base + cfg.packet_loss_per_100m * (distance_m / 100.0), 0.0, 1.0)
        per = np.where(self.bad_state, np.clip(base_per * cfg.burst_loss_multiplier, 0.0, 1.0), base_per)

        dropped = self._rng.random(n) < per
        # Drawn after the loss rolls, and only with jitter on, so a seeded run
        # without jitter replays exactly the packet-drop sequence it did before #207.
        if cfg.tdma_jitter_ms > 0.0:
            collided = self._slot_collisions(self._rng.normal(0.0, cfg.tdma_jitter_ms, n))
            self.packets_collided[collided] += 1
            dropped = dropped | collided
        self.packets_total += 1
        self.packets_dropped[dropped] += 1
        return dropped

    def _slot_collisions(self, offset_ms: np.ndarray) -> np.ndarray:
        """Which drones' packets were lost to a slot collision this frame.

        Args:
            offset_ms: Each drone's send-time error this frame, ms, shape ``(n,)``.
                Drone ``i`` owns slot ``i``.

        A late send (beyond the guard) collides with the next slot's packet,
        an early one with the previous slot's; both packets of a collision are
        lost. A lone drone has no neighbour, so it never collides.
        """
        n = offset_ms.shape[0]
        collided = np.zeros(n, dtype=bool)
        if n < 2:
            return collided
        guard = self.tdma_guard_ms
        late = offset_ms > guard
        early = offset_ms < -guard
        collided |= late | early
        collided |= np.roll(late, 1)  # drone i late hits drone i+1
        collided |= np.roll(early, -1)  # drone i early hits drone i-1
        return collided

    def drop_rate(self) -> np.ndarray:
        """Each drone's observed drop rate so far (dropped / total packets).

        ``0.0`` for a drone that hasn't ticked yet, not ``NaN`` — a GCS
        metric reading this before the fleet has started should see "no
        loss recorded yet", not a divide-by-zero artefact.
        """
        return np.divide(
            self.packets_dropped,
            self.packets_total,
            out=np.zeros(self.packets_dropped.shape, dtype=float),
            where=self.packets_total > 0,
        )
