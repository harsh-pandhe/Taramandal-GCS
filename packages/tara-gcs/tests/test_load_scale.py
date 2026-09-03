"""Fleet-scale load and latency budgets for the live safety/telemetry path.

Scope note, because this issue (#24) overlaps a merged one.
`tools/benchmark_fleet_scale.py` and `docs/fleet-scale-benchmark.md` (issue #3)
already measured *end-to-end* fleet behaviour and found the real-time ceiling is
~500 drones, not 1000: at N=1000 the event loop runs at 0.19x real time with 91%
of ticks late. Correctness holds there (every drone arms, zero breaches) but
timing does not.

This file therefore does **not** re-assert fleet-wide real-time viability at
1000 — that would contradict a merged measurement. It guards the *per-frame
component budgets* that are genuinely fast, and whose regression would be
silent:

  * the cKDTree proximity scan, which is the safety loop's per-frame cost, and
  * ``encode_fast_frame``, which is the per-client telemetry broadcast cost.

Both are cheap today. The point is to notice if that stops being true — the
specific regression worth catching is someone reintroducing an O(n^2) pairwise
loop, which at N=1000 means ~500k distance computations per frame instead of a
tree query, and would blow the budget by orders of magnitude while still passing
every correctness test.

On the 8 ms threshold: the issue frames it as guaranteeing "10 Hz loop closure",
but 10 Hz is a 100 ms budget, so 8 ms is not what makes the loop close. It is
used here as what it actually is — a tripwire at roughly 7x measured cost, tight
enough to catch an algorithmic regression and loose enough to survive a noisy
shared CI runner.

Timing on CI is inherently noisy, so every latency assertion here scores the
**median** of many samples rather than a single run or a mean. One descheduled
sample on a contended runner should not fail a build; a genuine 7x regression
moves the median and will.
"""

from __future__ import annotations

import asyncio
import statistics
import time

import numpy as np
import pytest
from scipy.spatial import cKDTree

from tara_core import DEFAULT_LIMITS
from tara_core.proximity import close_pairs, min_separation
from tara_gcs.telemetry_codec import FAST_HZ, encode_fast_frame, encode_slow_frame

# The issue's headline budget: one proximity frame at fleet scale.
PROXIMITY_BUDGET_MS = 8.0
FLEET_N = 1000

# Spread drones over a volume big enough that a 1.5m separation limit finds
# essentially nothing. A tree query's cost depends on how many pairs it has to
# *report*, so benchmarking on a cloud of mutually-violating points would measure
# the breach-reporting path rather than the clean-frame path the loop actually
# spends its life in.
FIELD_M = 300.0


def _fleet_positions(n: int, seed: int = 0) -> dict[int, tuple[float, float, float]]:
    """``n`` drones scattered through a FIELD_M cube, keyed by drone id."""
    rng = np.random.default_rng(seed)
    pts = rng.uniform(0.0, FIELD_M, size=(n, 3))
    return {i: (float(x), float(y), float(z)) for i, (x, y, z) in enumerate(pts)}


def _telemetry_for(positions: dict[int, tuple[float, float, float]]) -> dict[int, dict]:
    """A telemetry dict shaped exactly like ``DroneManager.telemetry``.

    Field-for-field with ``_connect_drone``'s initializer, because both codec
    frames and the proximity loop index these keys directly — a short dict would
    KeyError rather than measure anything.
    """
    tel = {}
    for d_id, (x, y, z) in positions.items():
        tel[d_id] = {
            "connected": True,
            "armed": True,
            "fleet_x": x,
            "fleet_y": y,
            "fleet_z": z,
            "local_x": x,
            "local_y": y,
            "local_z": z,
            "heading": 0.0,
            "battery_percent": 95.0,
            "battery_voltage": 22.2,
            "satellites": 14,
            "gps_lock": True,
            "gps_fix_type": "FIX_3D",
            "flight_mode": "OFFBOARD",
            "comm_latency_ms": 4.2,
            "home_lat": 0.0,
            "home_lon": 0.0,
        }
    return tel


def _median_ms(fn, samples: int = 30, warmup: int = 3) -> float:
    """Median wall-clock cost of ``fn`` in ms.

    Warms up first: the first call pays numpy/scipy allocator and cache costs
    that never recur in the running loop, and including it would inflate a small
    sample noticeably.
    """
    for _ in range(warmup):
        fn()
    timings = []
    for _ in range(samples):
        start = time.perf_counter()
        fn()
        timings.append((time.perf_counter() - start) * 1000.0)
    return statistics.median(timings)


class TestProximityLatency:
    """The issue's core assertion: a proximity frame at N=1000 stays under 8 ms."""

    def test_close_pairs_under_budget_at_1000_drones(self):
        positions = _fleet_positions(FLEET_N)
        median = _median_ms(lambda: close_pairs(positions, DEFAULT_LIMITS.min_separation_m))
        assert median < PROXIMITY_BUDGET_MS, (
            f"close_pairs at N={FLEET_N} took {median:.3f} ms "
            f"(budget {PROXIMITY_BUDGET_MS} ms). A jump this large usually means "
            "the cKDTree was replaced by a pairwise loop."
        )

    def test_live_monitor_scan_under_budget_at_1000_drones(self):
        """Benchmarks the scan ``_proximity_monitor_loop`` actually runs.

        Deliberately separate from the test above. ``DroneManager._proximity_monitor_loop``
        does **not** call ``tara_core.proximity.close_pairs`` — it builds its own
        ``cKDTree`` inline over ``fleet_x/y/z`` (drone_manager.py:731). So there
        are two distinct cKDTree paths in the system, and the one gating flight
        safety at 10 Hz is this one. Benchmarking only the shared helper would
        leave the live path unguarded.

        Mirrors the loop body's real work — active-drone filtering, coordinate
        assembly from the telemetry dict, tree construction, pair query — without
        the ``asyncio.sleep`` or the RTL trigger.
        """
        telemetry = _telemetry_for(_fleet_positions(FLEET_N))
        limit = DEFAULT_LIMITS.min_separation_m

        def scan():
            active_ids = [
                d_id for d_id, tel in telemetry.items() if tel["connected"] and tel["armed"]
            ]
            coords = [
                [telemetry[d]["fleet_x"], telemetry[d]["fleet_y"], telemetry[d]["fleet_z"]]
                for d in active_ids
            ]
            return cKDTree(coords).query_pairs(limit)

        median = _median_ms(scan)
        assert median < PROXIMITY_BUDGET_MS, (
            f"Live proximity scan at N={FLEET_N} took {median:.3f} ms "
            f"(budget {PROXIMITY_BUDGET_MS} ms)."
        )

    def test_min_separation_under_budget_at_1000_drones(self):
        positions = _fleet_positions(FLEET_N)
        median = _median_ms(lambda: min_separation(positions))
        assert median < PROXIMITY_BUDGET_MS, (
            f"min_separation at N={FLEET_N} took {median:.3f} ms."
        )

    def test_scaling_is_subquadratic(self):
        """Guards the O(n log n) claim by shape, not by absolute time.

        The absolute budgets above can be satisfied by a fast quadratic
        implementation on a quick machine, so they would miss a reintroduced
        O(n^2) loop on fast hardware. Cost growth from N=250 to N=1000 (4x the
        drones) would be ~16x if quadratic and ~4-5x if tree-based. 10x sits
        between those with room for timer noise at these small magnitudes.
        """
        limit = DEFAULT_LIMITS.min_separation_m
        small = _fleet_positions(250, seed=1)
        large = _fleet_positions(1000, seed=2)

        t_small = _median_ms(lambda: close_pairs(small, limit))
        t_large = _median_ms(lambda: close_pairs(large, limit))

        # Guard against a near-zero denominator making the ratio meaningless on a
        # machine where the small case lands under timer resolution.
        if t_small < 0.05:
            pytest.skip(f"N=250 case too fast to time reliably ({t_small:.4f} ms)")

        assert t_large / t_small < 10.0, (
            f"Proximity cost grew {t_large / t_small:.1f}x for a 4x fleet increase "
            f"({t_small:.3f} ms -> {t_large:.3f} ms), which looks quadratic."
        )

    def test_breaches_are_still_detected_at_fleet_scale(self):
        """A fast scan that finds nothing would pass every budget above.

        Pins correctness to the same scale as the timing: plant one known-close
        pair in a 1000-drone field and require it to come back.
        """
        positions = _fleet_positions(FLEET_N)
        positions[0] = (10.0, 10.0, 10.0)
        positions[1] = (10.0, 10.5, 10.0)  # 0.5 m apart, well inside any limit

        found = close_pairs(positions, DEFAULT_LIMITS.min_separation_m)

        assert (0, 1) in [(a, b) for a, b, _ in found]
        assert min_separation(positions) == pytest.approx(0.5, abs=1e-6)


class TestSyntheticTelemetryLoad:
    """The 10 Hz synthetic load generator from the issue."""

    async def test_sustains_10hz_updates_for_1000_drones(self):
        """1000 generators pushing state at 10 Hz into a shared telemetry dict.

        Models the ingest side of the load: what the monitor loops contend with
        while the fleet streams. Asserts the aggregate update rate holds, since
        falling behind here means the safety loops are reading stale positions.

        Runs a short fixed number of ticks rather than a wall-clock duration so
        it stays deterministic and fast enough for the default suite.
        """
        n_drones, hz, ticks = FLEET_N, 10.0, 10
        interval = 1.0 / hz
        telemetry = _telemetry_for(_fleet_positions(n_drones))
        counter = {"updates": 0}

        async def generator(drone_id: int):
            for tick in range(ticks):
                t = telemetry[drone_id]
                t["fleet_x"] = float(tick)
                t["fleet_y"] = float(drone_id)
                t["fleet_z"] = -10.0
                counter["updates"] += 1
                await asyncio.sleep(interval)

        start = time.perf_counter()
        await asyncio.gather(*(generator(i) for i in range(n_drones)))
        elapsed = time.perf_counter() - start

        expected = n_drones * ticks
        assert counter["updates"] == expected

        # The floor is what the schedule alone costs (ticks * interval); anything
        # on top is scheduling overhead. Generous multiplier because Windows'
        # ~15.6 ms timer granularity alone stretches a 100 ms sleep measurably,
        # and CI runners are shared — see docs/fleet-scale-benchmark.md.
        floor = ticks * interval
        assert elapsed < floor * 3.0, (
            f"{n_drones} generators at {hz} Hz took {elapsed:.2f}s "
            f"against a {floor:.2f}s scheduling floor."
        )

        rate = counter["updates"] / elapsed
        assert rate > n_drones * hz * 0.3, f"Aggregate ingest fell to {rate:.0f} updates/s."

    async def test_proximity_scan_keeps_up_with_live_updates(self):
        """The realistic version of the budget: scanning while positions move.

        The timing tests above scan a frozen dict. Here the safety scan runs at
        10 Hz against telemetry being rewritten underneath it, which is the actual
        production interleaving — and the case where a scan that is individually
        fast can still fall behind.
        """
        telemetry = _telemetry_for(_fleet_positions(FLEET_N))
        limit = DEFAULT_LIMITS.min_separation_m
        scan_times: list[float] = []
        stop = asyncio.Event()

        async def mover():
            while not stop.is_set():
                for d_id in range(0, FLEET_N, 10):  # stride: enough churn, bounded cost
                    telemetry[d_id]["fleet_x"] += 0.1
                await asyncio.sleep(0.05)

        async def scanner():
            for _ in range(10):
                start = time.perf_counter()
                active = [d for d, t in telemetry.items() if t["connected"] and t["armed"]]
                coords = [
                    [telemetry[d]["fleet_x"], telemetry[d]["fleet_y"], telemetry[d]["fleet_z"]]
                    for d in active
                ]
                cKDTree(coords).query_pairs(limit)
                scan_times.append((time.perf_counter() - start) * 1000.0)
                await asyncio.sleep(0.1)

        mover_task = asyncio.create_task(mover())
        try:
            await scanner()
        finally:
            stop.set()
            await mover_task

        median = statistics.median(scan_times)
        assert median < PROXIMITY_BUDGET_MS, (
            f"Under concurrent telemetry updates the proximity scan took "
            f"{median:.3f} ms (budget {PROXIMITY_BUDGET_MS} ms)."
        )


class TestTelemetryBroadcastCost:
    """Per-client broadcast cost, and what it implies for client fan-out.

    The issue asks to assert "1000 connected clients with zero dropped frames".
    That is not assertable against the current endpoint, and these tests measure
    why rather than asserting something weaker and calling it the same thing.

    ``/ws/telemetry`` (main.py:314) runs an independent ``while True`` loop per
    client, each re-encoding the whole fleet frame — there is no shared
    broadcast, so encode cost scales linearly with client count. At the measured
    ~1 ms per 1000-drone frame and FAST_HZ=20, 1000 clients would need ~20
    seconds of CPU per second of wall-clock and ~720 MB/s of egress. asyncio is
    single-threaded, so that is roughly 20x over a one-core budget regardless of
    machine.

    Supporting 1000 clients is a redesign — encode one frame per tick fleet-wide
    and fan the same buffer out — not a test. What is guarded here is the
    per-frame cost that redesign would depend on, plus the client count the
    current design genuinely supports.
    """

    def test_fast_frame_encode_cost_at_fleet_scale(self):
        """One fast frame must fit well inside its own 1/FAST_HZ slot."""
        telemetry = _telemetry_for(_fleet_positions(FLEET_N))
        median = _median_ms(lambda: encode_fast_frame(telemetry))
        budget_ms = (1.0 / FAST_HZ) * 1000.0

        assert median < budget_ms * 0.25, (
            f"encode_fast_frame at N={FLEET_N} took {median:.3f} ms, more than a "
            f"quarter of the {budget_ms:.0f} ms tick budget — one client alone "
            "would crowd out the tick."
        )

    def test_slow_frame_encode_cost_at_fleet_scale(self):
        telemetry = _telemetry_for(_fleet_positions(FLEET_N))
        median = _median_ms(lambda: encode_slow_frame(telemetry), samples=10)
        assert median < 100.0, (
            f"encode_slow_frame at N={FLEET_N} took {median:.3f} ms against its 1 Hz slot."
        )

    def test_realistic_client_fanout_fits_the_tick_budget(self):
        """The client count the current per-client design actually supports.

        Eight concurrent operator screens is a realistic upper bound for a ground
        station; 1000 is not (browser clients and drones are unrelated axes). This
        asserts the honest version of the issue's criterion: at a plausible client
        count, a full fan-out round fits inside one FAST_HZ tick, so no client
        falls behind and no frames queue up.
        """
        clients = 8
        telemetry = _telemetry_for(_fleet_positions(FLEET_N))
        per_frame = _median_ms(lambda: encode_fast_frame(telemetry))
        budget_ms = (1.0 / FAST_HZ) * 1000.0

        assert per_frame * clients < budget_ms, (
            f"{clients} clients x {per_frame:.3f} ms = {per_frame * clients:.1f} ms "
            f"exceeds the {budget_ms:.0f} ms tick budget."
        )

    def test_thousand_client_fanout_exceeds_one_core(self):
        """Pins the ceiling documented above, so it cannot silently stop being true.

        Not a pass/fail on the endpoint — it records *why* the 1000-client
        criterion was not asserted. If a shared-broadcast refactor ever lands,
        this test should start failing and be replaced by a real 1000-client
        assertion. Until then it keeps the reasoning executable rather than
        stranded in a comment.
        """
        telemetry = _telemetry_for(_fleet_positions(FLEET_N))
        per_frame_s = _median_ms(lambda: encode_fast_frame(telemetry)) / 1000.0

        cpu_seconds_per_second = per_frame_s * 1000 * FAST_HZ

        assert cpu_seconds_per_second > 1.0, (
            "1000-client fan-out now fits in one core — the per-client encode "
            "design may have been replaced by a shared broadcast. If so, this "
            "test should be replaced with a real 1000-client assertion."
        )
