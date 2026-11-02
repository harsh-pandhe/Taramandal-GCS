"""Kill the GCS mid-hover and watch what PX4 does (issue #171).

Usage: TARA_PASSPHRASE=... python kill_case.py SIGKILL|SIGTERM [watch_seconds]
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, "/scratch")
import lib  # noqa: E402
from lib import req, ts  # noqa: E402
from observer import Observer  # noqa: E402

SIGNAL_NAME = sys.argv[1]
SIG = getattr(signal, SIGNAL_NAME)
WATCH_S = float(sys.argv[2]) if len(sys.argv) > 2 else 90.0
SERVER = "mavsdk_server -p 5005[1]"  # the GCS's drone 0; brackets keep pgrep off this line


def pids(pattern):
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout
    return [int(x) for x in out.split()]


async def blocking(fn, *args, **kwargs):
    return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args, **kwargs))


async def main():
    obs = Observer()
    await asyncio.wait_for(obs.start(), 40)
    if obs.mode != "HOLD" and not obs.armed:
        await obs.s.action.hold()
        await obs.wait_for(lambda: obs.mode == "HOLD", 10, "HOLD")
    print(ts(), "COM_OF_LOSS_T", await obs.s.param.get_param_float("COM_OF_LOSS_T"), flush=True)
    for name in ("NAV_DLL_ACT", "COM_OBL_RC_ACT", "COM_RC_IN_MODE", "COM_RCL_EXCEPT"):
        try:
            print(ts(), name, await obs.s.param.get_param_int(name), flush=True)
        except Exception as e:
            print(ts(), name, "ERR", e, flush=True)

    token = lib.login(os.environ.get("TARA_OPERATOR", "cmdr"), os.environ["TARA_PASSPHRASE"])
    ready = await blocking(lib.wait_ready, token)
    print(ts(), "preflight ready", ready, lib.status(), flush=True)
    assert ready

    async def hover():
        try:
            body = {"target_altitude_m": 2.0, "hover_duration_s": 30.0}
            code, _ = await blocking(req, "POST", "/api/single-drone/hover-sequence", body, token=token)
            print(ts(), "hover result", code, flush=True)
        except Exception as e:
            print(ts(), "hover http ended:", type(e).__name__, flush=True)

    hover_task = asyncio.create_task(hover())
    await obs.wait_for(lambda: obs.alt > 1.5, 60, "airborne")
    await asyncio.sleep(3)
    print(ts(), f"pre-kill mode={obs.mode} armed={obs.armed} alt={obs.alt:.2f}", flush=True)
    gcs = pids("[g]cs_run.py")[0]
    print(ts(), "gcs", gcs, "server", pids(SERVER), flush=True)
    os.kill(gcs, SIG)
    killed_at = time.time()
    print(ts(), "SENT", SIGNAL_NAME, "alt", round(obs.alt, 2), flush=True)

    server_gone = left_offboard = None
    last_tick = None
    while time.time() - killed_at < WATCH_S:
        await asyncio.sleep(0.1)
        elapsed = time.time() - killed_at
        if server_gone is None and not pids(SERVER):
            server_gone = elapsed
            print(ts(), f"server gone +{elapsed:.2f}s", flush=True)
        if left_offboard is None and obs.mode != "OFFBOARD":
            left_offboard = elapsed
            print(ts(), f"PX4 left OFFBOARD for {obs.mode} +{elapsed:.2f}s", flush=True)
        if int(elapsed) % 5 == 0 and int(elapsed) != last_tick:
            last_tick = int(elapsed)
            print(
                ts(),
                f"  +{elapsed:4.1f}s mode={obs.mode} armed={obs.armed} alt={obs.alt:.2f} "
                f"landed={obs.landed} server={pids(SERVER)}",
                flush=True,
            )
        if obs.armed is False:
            print(ts(), f"disarmed +{elapsed:.1f}s alt={obs.alt:.2f} landed={obs.landed}", flush=True)
            break
    print(json.dumps({
        "signal": SIGNAL_NAME, "server_gone_s": server_gone, "left_offboard_s": left_offboard,
        "final_mode": obs.mode, "armed": obs.armed, "alt": round(obs.alt, 2),
    }), flush=True)

    hover_task.cancel()
    if obs.armed:
        print(ts(), "cleanup land via observer", flush=True)
        try:
            await obs.s.action.land()
        except Exception as e:
            print("land err", e)
        await obs.wait_for(lambda: obs.armed is False, 60, "disarm")
    if obs.mode != "HOLD":
        try:
            await obs.s.action.hold()
        except Exception as e:
            print("hold err", e)
    await asyncio.sleep(1)


async def run():
    try:
        await asyncio.wait_for(main(), 300)
    finally:
        subprocess.run(["pkill", "-f", "mavsdk_server -p 5009[9]"])  # the observer's server
        os._exit(0)


if __name__ == "__main__":
    asyncio.run(run())
