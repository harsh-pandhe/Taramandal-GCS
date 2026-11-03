"""B-1 (review of #211): a pilot takes over while the sequence's own landing is under way.
Hover to 4 m, wait until the GCS has put PX4 in LAND, then take over with MANUAL_CONTROL +
start_position_control() (POSCTL). Then watch 25 s of PX4's own mode/armed stream."""

import asyncio
import json
import sys
import time

sys.path.insert(0, "/scratch")
import lib
from lib import req, ts
from observer import Observer


async def blocking(fn, *a, **k):
    return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*a, **k))


async def main(label):
    o = Observer()
    await asyncio.wait_for(o.start(), 40)
    cmdr = lib.login("cmdr", "cmdr passphrase")
    if o.mode != "HOLD" and not o.armed:
        try:
            await o.s.action.hold()
        except Exception as e:
            print("restore HOLD err", e)
    print(ts(), "preflight ready:", await blocking(lib.wait_ready, cmdr), flush=True)
    out = {}

    async def run():
        out["t0"] = time.time()
        out["res"] = await blocking(
            req,
            "POST",
            "/api/single-drone/hover-sequence",
            {"target_altitude_m": 4.0, "hover_duration_s": 2.0},
            token=cmdr,
            timeout=300,
        )
        out["t1"] = time.time()
        print(
            ts(),
            "HOVER HTTP RESULT:",
            out["res"][0],
            json.dumps(out["res"][1])[:600],
            flush=True,
        )

    t = asyncio.create_task(run())
    await o.wait_for(lambda: o.alt > 3.5, 90, "at altitude")
    ok = await o.wait_for(lambda: o.mode == "LAND", 60, "GCS lands")
    print(
        ts(),
        f"GCS landing under way: mode={o.mode} alt={o.alt:.2f} ok={ok}",
        flush=True,
    )
    mc = o.s.manual_control
    stop = asyncio.Event()

    async def feed():
        while not stop.is_set():
            try:
                await mc.set_manual_control_input(0.0, 0.0, 0.5, 0.0)
            except Exception as e:
                print("feed err", e)
            await asyncio.sleep(0.05)

    feeder = asyncio.create_task(feed())
    await asyncio.sleep(0.3)
    t_take = time.time()
    print(ts(), f"TAKEOVER start_position_control() alt={o.alt:.2f}", flush=True)
    try:
        await mc.start_position_control()
    except Exception as e:
        print(ts(), "start_position_control FAILED", e, flush=True)
    for k in range(25):
        await asyncio.sleep(1)
        print(
            ts(),
            f"+{time.time() - t_take:4.1f}s mode={o.mode} armed={o.armed} alt={o.alt:.2f} landed={o.landed} http_done={t.done()}",
            flush=True,
        )
    print(
        ts(),
        f"RESULT[{label}] mode_at_25s={o.mode} armed={o.armed} alt={o.alt:.2f}",
        flush=True,
    )
    if not t.done():
        try:
            await asyncio.wait_for(t, 60)
        except Exception as e:
            print("hover task", e)
    stop.set()
    feeder.cancel()
    try:
        await o.s.action.land()
    except Exception as e:
        print("cleanup land err", e)
    await o.wait_for(lambda: o.armed is False, 60, "disarm in cleanup")
    try:
        await o.s.action.hold()
    except Exception as e:
        print("cleanup hold err", e)
    for line in open("/scratch/cmd_audit.jsonl").read().splitlines()[-8:]:
        print("  AUDIT", line[:240])


asyncio.run(main(sys.argv[1]))
