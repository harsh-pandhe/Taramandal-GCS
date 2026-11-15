#!/usr/bin/env python3
"""Records a 15-minute, 100-drone flight with the real black-box recorder and tara_sim, then reports what the
replay API makes of it (issue #65).

It writes two files into --out (default ./replay-perf):
  blackbox-perf100.jsonl.gz   the recording (what the GCS would have written)
  overview.json               the three replay API replies a browser gets when it loads that recording
                              (summary, safety events, the default whole-session frame overview)
  window.json                 one window at the full 10 Hz, sized as the replay sizes it (30,000 samples: 30 s at 100
                              drones, 3 s at 1,000), i.e. what the replay's zoom asks the GCS for

Then run the browser half against it:
  REPLAY_PERF_DATA=<out>/overview.json REPLAY_PERF_WINDOW=<out>/window.json \
      npx playwright test e2e/replay-perf.spec.js   (in packages/tara-web)

Usage, from the repo root:  python tools/replay_perf_record.py [--out DIR] [--drones 100] [--minutes 15]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "packages" / p) for p in ("tara-gcs/tests", "tara-gcs", "tara-core", "tara-sim")]

import numpy as np  # noqa: E402
import test_blackbox as tb  # noqa: E402  (its _sim_fleet/_fly_one_tick drive the real physics, as the 25 MB test does)
from tara_gcs import blackbox_replay as br  # noqa: E402
from tara_gcs.blackbox import BlackboxRecorder  # noqa: E402

#: The replay's full-rate window budget and bounds (packages/tara-web/src/replay.js: DETAIL_SAMPLE_BUDGET,
#: detailSpanMs). Kept in step by hand: it only decides how big a window this tool writes.
DETAIL_SAMPLE_BUDGET = 30_000
TICK_MS = 100


def detail_span_ms(drones: int) -> int:
    return min(60_000, max(3_000, (DETAIL_SAMPLE_BUDGET // max(1, drones)) * TICK_MS))


async def record(path: str, n_drones: int, minutes: int) -> None:
    dm, fs = await tb._sim_fleet(n_drones, sim_packet_loss_base=0.01, sim_packet_loss_per_100m=0.02)
    clock = tb.FakeClock()
    rec = BlackboxRecorder(path, clock=clock, fsync_interval_s=None,
                           context=lambda: {"backend": dm.backend, "fidelity": dm.fidelity})
    phases = np.arange(n_drones) * 0.37
    started = time.time()
    try:
        for k in range(minutes * 600):
            tb._fly_one_tick(dm, fs, phases, k * 0.1, k)
            rec.record_tick(dm.telemetry, rf_drop_rate=fs.rf_channel.drop_rate())
            if k % 600 == 300:
                rec.record_command("hold", operator="bench", trigger="bench")
            # a synthetic breach and clear every 3 minutes, so the timeline has markers to pin
            if k % 1800 == 900:
                rec.record_event("proximity_breach", drones=[3, 4], distance_m=1.4)
            if k % 1800 == 1000:
                rec.record_event("proximity_clear", drones=[3, 4])
            clock.advance()
    finally:
        rec.close()
        await dm.shutdown()
    print(f"recorded {rec.ticks} ticks, {os.path.getsize(path) / 1e6:.2f} MB, in {time.time() - started:.0f} s of wall time")


def report(path: str, out: Path, n_drones: int) -> None:
    t = time.perf_counter()
    session = br.ReplaySession.load(path, "perf")
    print(f"decode of the whole recording: {time.perf_counter() - t:.2f} s ({len(session.ticks)} ticks)")
    t = time.perf_counter()
    overview = session.frames()
    built = (time.perf_counter() - t) * 1000
    print(f"default overview: {overview['count']} frames, stride {overview['stride']}, "
          f"{len(json.dumps(overview)) / 1e6:.2f} MB of JSON, built in {built:.0f} ms")
    span = detail_span_ms(n_drones)
    from_ms = 300_000
    for step in (1000, 500, 0):
        t = time.perf_counter()
        window = session.frames(from_ms=from_ms, to_ms=from_ms + span, step_ms=step, max_frames=5000, max_samples=200_000)
        label = f"step {step} ms" if step else "every tick"
        print(f"{span / 1000:.0f} s window at {label}: {window['count']} frames, stride {window['stride']}, "
              f"{len(json.dumps(window)) / 1e6:.2f} MB, {(time.perf_counter() - t) * 1000:.0f} ms")
    # One window at the full 10 Hz, for e2e/replay-perf.spec.js (REPLAY_PERF_WINDOW) to serve as the zoom reply.
    (out / "window.json").write_text(json.dumps(
        session.frames(from_ms=from_ms, to_ms=from_ms + span, max_frames=5000, max_samples=200_000)))
    (out / "overview.json").write_text(json.dumps({
        "summary": session.summary(),
        "events": session.event_list(br.SAFETY_EVENT_KINDS),
        "frames": overview,
    }))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path("replay-perf"))
    ap.add_argument("--drones", type=int, default=100)
    ap.add_argument("--minutes", type=int, default=15)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    path = str(args.out / f"blackbox-perf{args.drones}.jsonl.gz")
    asyncio.run(record(path, args.drones, args.minutes))
    report(path, args.out, args.drones)


if __name__ == "__main__":
    main()
