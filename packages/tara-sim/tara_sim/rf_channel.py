"""A configurable RF channel model for `tara_sim` (issue #62).

`FleetState` already has a connectivity dropout knob
(`SimConfig.dropout_prob_per_tick`): a flat, distance-independent probability
that a drone's `connected` flag *flips* each tick — a memoryless telegraph
process, good enough to exercise failsafe code paths but not shaped like a
real radio link, whose loss grows with distance and comes in bursts (fading,
interference) rather than independent coin-flips.

`RFChannelModel` is a separate, opt-in mechanism `FleetState` composes
instead of (never alongside — see `FleetState.__init__`'s validation) the
flat dropout, built from two pieces:

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

**Not modeled here: TDMA slot jitter / inter-slot collisions** (the third
item in #62's scope). There is no TDMA scheduler anywhere in this codebase
to perturb — the broadcast radio (`SimFleet.broadcast`) is a single shared
channel with no slot structure at all. Modeling TDMA collisions means first
designing a TDMA scheme (slot count, frame duration, collision resolution),
which is a protocol decision for a follow-up issue, not an implementation of
something already specified here.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


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
    """

    packet_loss_base: float = 0.0
    packet_loss_per_100m: float = 0.0
    burst_loss_multiplier: float = 4.0
    p_good_to_bad: float = 0.01
    p_bad_to_good: float = 0.3

    def __post_init__(self) -> None:
        for name in ("packet_loss_base", "packet_loss_per_100m", "p_good_to_bad", "p_bad_to_good"):
            value = getattr(self, name)
            if value < 0.0:
                raise ValueError(f"{name} must be >= 0, got {value}")
        if self.burst_loss_multiplier < 1.0:
            raise ValueError(f"burst_loss_multiplier must be >= 1.0, got {self.burst_loss_multiplier}")

    @property
    def enabled(self) -> bool:
        """Whether this config would ever drop a packet.

        `FleetState` checks this to decide whether to build a model at all —
        an all-zero config is a no-op by construction, not just by never
        rolling a loss, so a caller that constructs `RFChannelConfig()` and
        never touches it gets exactly pre-#62 behaviour.
        """
        return self.packet_loss_base > 0.0 or self.packet_loss_per_100m > 0.0


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
        self.bad_state = np.zeros(n, dtype=bool)
        #: Per-drone running counts, for the GCS-facing "live packet drop
        #: rate" metric (see `drop_rate`) — not reset by anything short of
        #: recreating this model.
        self.packets_dropped = np.zeros(n, dtype=np.int64)
        self.packets_total = np.zeros(n, dtype=np.int64)

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
        self.packets_total += 1
        self.packets_dropped[dropped] += 1
        return dropped

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
