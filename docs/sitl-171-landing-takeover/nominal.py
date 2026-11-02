import asyncio, json, sys, time
sys.path.insert(0, "/scratch")
import lib
from lib import req, ts
from observer import Observer
async def blocking(fn, *a, **k):
    return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*a, **k))
async def main():
    o = Observer(); await asyncio.wait_for(o.start(), 40)
    cmdr = lib.login("cmdr", "cmdr passphrase")
    if o.mode != "HOLD" and not o.armed:
        try: await o.s.action.hold()
        except Exception as e: print("restore HOLD err", e)
    print(ts(), "ready:", await blocking(lib.wait_ready, cmdr), flush=True)
    s, b = await blocking(req, "POST", "/api/single-drone/hover-sequence", {"target_altitude_m": 4.0, "hover_duration_s": 2.0}, token=cmdr, timeout=300)
    print(ts(), "NOMINAL HTTP", s, json.dumps(b)[:200], flush=True)
    print(ts(), "modes seen:", [e[2] for e in o.events if e[1] == "mode"], "armed now:", o.armed, flush=True)
    await asyncio.sleep(2)
asyncio.run(main())
