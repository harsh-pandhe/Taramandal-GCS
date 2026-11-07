"""#171 acceptance scenarios, one per matrix row. Each runs against a live stack inside a fresh
container and writes $OUT/result.json: explicit checks (name, ok, detail), notes, the observer's
event log and a 5 Hz position/velocity timeline. Ground truth comes from an independent MAVSDK
client on PX4's GCS port (14550), not from the GCS. Usage: scenarios.py CASE"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid

sys.path.insert(0, "/scratch")
import lib
from lib import req, ts
from observer import Observer

OUT = os.environ.get("OUT", "/scratch/out/adhoc")
R = {"checks": [], "notes": {}}


def check(name, ok, detail=""):
    R["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(ts(), "CHECK", "PASS" if ok else "FAIL", name, "|", detail, flush=True)


def note(k, v):
    R["notes"][k] = v
    print(ts(), "NOTE", k, "=", json.dumps(v)[:300], flush=True)


async def blocking(fn, *a, **k):
    return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*a, **k))


def brief():
    st = lib.status()
    return {k: st.get(k) for k in ("connected", "armed", "armed_stale", "armed_stale_ids", "fleet_state")}


def px4_pid():
    out = subprocess.run(["pgrep", "-f", "px4_sitl_default/bin/px[4] -d"], capture_output=True, text=True).stdout.split()
    return int(out[0])


OBS = None


async def setup(wait_ready=True):
    global OBS
    o = Observer()
    OBS = o
    await asyncio.wait_for(o.start(), 60)
    cmdr = lib.login("cmdr", "cmdr passphrase")
    chor = lib.login("chor", "chor passphrase")
    if wait_ready:
        ok = await blocking(lib.wait_ready, cmdr)
        check("preflight ready before the run", ok, brief())
    return o, cmdr, chor


def hover_task(cmdr, alt=1.0, hold=5.0, out=None):
    out = out if out is not None else {}

    async def run():
        out["t0"] = time.time()
        out["res"] = await blocking(
            req, "POST", "/api/single-drone/hover-sequence",
            {"target_altitude_m": alt, "hover_duration_s": hold}, token=cmdr, timeout=300,
        )
        out["t1"] = time.time()
        print(ts(), "HOVER HTTP RESULT:", out["res"][0], json.dumps(out["res"][1])[:500], flush=True)

    return asyncio.create_task(run()), out


def steps_of(res):
    body = res[1] or {}
    return {s["step"]: s for s in body.get("steps", [])} if isinstance(body, dict) else {}


async def cleanup(o):
    """Best effort: leave the vehicle landed and disarmed (the container is removed right after)."""
    try:
        if o.armed or o.alt > 0.3:
            await o.s.action.land()
        await o.wait_for(lambda: o.armed is False, 60, "disarm in cleanup")
        if o.mode != "HOLD":
            await o.s.action.hold()
    except Exception as e:  # noqa: BLE001
        print("cleanup:", e, flush=True)


def phase_samples(o, mode):
    return [s for s in o.samples if s[3] == mode]


# ---------------------------------------------------------------- nominal
async def case_nominal():
    o, cmdr, _ = await setup()
    t, out = hover_task(cmdr, 1.0, 5.0)
    await t
    s, b = out["res"]
    st = steps_of(out["res"])
    check("hover-sequence returns 200 COMPLETED", s == 200 and (b or {}).get("status") == "COMPLETED", f"{s} {(b or {}).get('status')}")
    note("steps", {k: {kk: vv for kk, vv in v.items() if kk != "time"} for k, v in st.items()})
    check("every step present", all(k in st for k in ("PREFLIGHT", "ARM", "HOVER_REACHED", "HOVER_HOLD_COMPLETE", "LANDING_COMMANDED", "TOUCHDOWN_DISARM")), list(st))
    await asyncio.sleep(2)
    top = max((x[1] for x in o.samples), default=0.0)
    check("reached about 1 m (0.8 to 1.4 m on PX4's own estimate)", 0.8 <= top <= 1.4, f"peak {top:.2f} m")
    check("PX4 ends ON_GROUND and disarmed", o.landed == "ON_GROUND" and o.armed is False, f"landed={o.landed} armed={o.armed}")
    land = [x[2] for x in phase_samples(o, "LAND") if x[1] > 0.15]
    vmax = max(land, default=0.0)
    note("landing_max_descent_m_s", round(vmax, 3))
    check("slow landing: peak descent rate <= 1.0 m/s (PX4 MPC_LAND_SPEED is 0.7)", land and vmax <= 1.0, f"peak {vmax:.2f} m/s over {len(land)} samples")
    b2 = brief()
    check("GCS agrees: armed 0, armed_stale 0, connected 1", (b2["armed"], b2["armed_stale"], b2["connected"]) == (0, 0, 1), b2)
    note("total_s", round(out["t1"] - out["t0"], 1))


# ---------------------------------------------------------------- takeover
async def _feed_and_takeover(o):
    mc = o.s.manual_control
    stop = asyncio.Event()

    async def feed():
        while not stop.is_set():
            try:
                await mc.set_manual_control_input(0.0, 0.0, 0.5, 0.0)
            except Exception as e:  # noqa: BLE001
                print("feed err", e)
            await asyncio.sleep(0.05)

    feeder = asyncio.create_task(feed())
    await asyncio.sleep(0.5)
    t_take = time.time()
    print(ts(), f"TAKEOVER start_position_control() alt={o.alt:.2f} mode={o.mode}", flush=True)
    try:
        await mc.start_position_control()
    except Exception as e:  # noqa: BLE001
        print(ts(), "start_position_control FAILED", e, flush=True)
    return stop, feeder, t_take


async def case_takeover_dwell():
    o, cmdr, _ = await setup()
    t, out = hover_task(cmdr, 1.0, 20.0)
    await o.wait_for(lambda: o.alt > 0.7, 60, "airborne")
    await asyncio.sleep(3)
    stop, feeder, t_take = await _feed_and_takeover(o)
    n0 = len(o.events)
    await asyncio.sleep(15)
    modes_after = [e[2] for e in o.events[n0:] if e[1] == "mode"]
    note("method", "MAVSDK manual_control MANUAL_CONTROL stream, then start_position_control() (OFFBOARD to POSCTL)")
    note("mode_events_after_takeover", modes_after)
    check("PX4 stays in POSCTL and armed 15 s after the switch", o.mode == "POSCTL" and o.armed is True, f"mode={o.mode} armed={o.armed} alt={o.alt:.2f}")
    check("GCS sent no LAND or other mode change after the takeover", "LAND" not in modes_after and "HOLD" not in modes_after, modes_after)
    if not t.done():
        await asyncio.wait_for(t, 60)
    s, b = out["res"]
    check("hover-sequence stops with 500 'operator took over'", s == 500 and "operator took over" in json.dumps(b), f"{s} {json.dumps(b)[:160]}")
    check("sequence stopped within 2 s of the switch", out["t1"] - t_take < 2.5, f"{out['t1'] - t_take:.2f} s")
    stop.set()
    feeder.cancel()
    await cleanup(o)


async def case_takeover_landing():
    o, cmdr, _ = await setup()
    t, out = hover_task(cmdr, 4.0, 2.0)
    await o.wait_for(lambda: o.alt > 3.5, 90, "at altitude")
    ok = await o.wait_for(lambda: o.mode == "LAND", 60, "GCS lands")
    check("GCS had put PX4 in LAND", ok, f"alt {o.alt:.2f}")
    stop, feeder, t_take = await _feed_and_takeover(o)
    n0 = len(o.events)
    await asyncio.sleep(25)
    modes_after = [e[2] for e in o.events[n0:] if e[1] == "mode"]
    note("mode_events_after_takeover", modes_after)
    check("PX4 stays in POSCTL, armed and airborne for 25 s (the GCS does not re-send land)", o.mode == "POSCTL" and o.armed is True and o.alt > 2.0, f"mode={o.mode} armed={o.armed} alt={o.alt:.2f}")
    if not t.done():
        await asyncio.wait_for(t, 60)
    s, b = out["res"]
    check("hover-sequence stops with 500 'operator took over during the landing'", s == 500 and "took over during the landing" in json.dumps(b), f"{s} {json.dumps(b)[:200]}")
    stop.set()
    feeder.cancel()
    await cleanup(o)


# ---------------------------------------------------------------- link loss
async def case_link_kill():
    """Issue's method: stop the PX4 process mid-hover (SIGKILL; SIGSTOP of a flying vehicle corrupts lockstep SITL)."""
    o, cmdr, _ = await setup()
    t, out = hover_task(cmdr, 1.0, 30.0)
    await o.wait_for(lambda: o.alt > 0.7, 60, "airborne")
    await asyncio.sleep(3)
    b0 = brief()
    check("baseline mid-hover: connected 1, armed 1, armed_stale 0", (b0["connected"], b0["armed"], b0["armed_stale"]) == (1, 1, 0), b0)
    pid = px4_pid()
    os.kill(pid, signal.SIGKILL)
    t_k = time.time()
    print(ts(), "PX4 SIGKILL mid-hover pid", pid, flush=True)
    t_conn0 = t_stale = None
    for _ in range(80):
        b = brief()
        if t_conn0 is None and b["connected"] == 0:
            t_conn0 = time.time() - t_k
        if t_stale is None and b["armed"] == 0 and b["armed_stale"] == 1:
            t_stale = time.time() - t_k
        if t_conn0 is not None and t_stale is not None:
            break
        await asyncio.sleep(0.25)
    note("seconds_to_connected_0", t_conn0)
    note("seconds_to_armed_stale_1", t_stale)
    check("connected falls to 0 within 6 s", t_conn0 is not None and t_conn0 <= 6.0, t_conn0)
    check("armed-but-silent is visible: armed 0 and armed_stale 1 within 6 s", t_stale is not None and t_stale <= 6.0, (t_stale, brief()))
    await asyncio.wait_for(t, 120)
    s, b = out["res"]
    note("hover_result", {"http": s, "detail": json.dumps(b)[:300]})
    check("hover-sequence fails closed (not 200 COMPLETED) with a reason", s != 200 and bool(b), f"{s} {json.dumps(b)[:200]}")
    check("fleet never reports the dead drone as connected afterwards", brief()["connected"] == 0, brief())


async def case_reconnect(settle=False):
    o, cmdr, _ = await setup()
    pid = px4_pid()
    b0 = brief()
    os.kill(pid, signal.SIGSTOP)
    t_s = time.time()
    print(ts(), "PX4 SIGSTOP on the ground", flush=True)
    t_drop = None
    while time.time() - t_s < 8:
        if t_drop is None and brief()["connected"] == 0:
            t_drop = time.time() - t_s
        await asyncio.sleep(0.25)
    os.kill(pid, signal.SIGCONT)
    t_c = time.time()
    print(ts(), "PX4 SIGCONT", flush=True)
    t_back = None
    while time.time() - t_c < 40:
        if brief()["connected"] == 1:
            t_back = time.time() - t_c
            break
        await asyncio.sleep(0.25)
    note("seconds_frozen_until_connected_0", t_drop)
    note("seconds_after_SIGCONT_until_connected_1", t_back)
    check("baseline connected 1 before the freeze", b0["connected"] == 1, b0)
    check("connected drops during an 8 s freeze (within 5 s)", t_drop is not None and t_drop <= 5.0, t_drop)
    check("connected is back within 15 s of SIGCONT", t_back is not None and t_back <= 15.0, t_back)
    ok = await blocking(lib.wait_ready, cmdr, 90)
    check("preflight green again after the reconnect", ok, brief())
    note("px4_estimate_when_preflight_went_green", {"alt_m": round(o.alt, 2), "vel_down_m_s": round(o.vd, 2), "armed": o.armed})
    if settle:
        # The first attempt (run `reconnect`) armed while PX4's altitude estimate was still decaying
        # from the freeze (disarmed on the ground, estimate +5.7 m at worst). Wait for it to settle.
        t_w = time.time()
        quiet_since = None
        while time.time() - t_w < 60:
            if abs(o.alt) < 0.15 and abs(o.vd) < 0.1:
                quiet_since = quiet_since or time.time()
                if time.time() - quiet_since > 3:
                    break
            else:
                quiet_since = None
            await asyncio.sleep(0.2)
        note("seconds_for_px4_estimate_to_settle_after_SIGCONT", round(time.time() - t_c, 1))
        check("PX4's altitude estimate settles to the ground (|alt|<0.15 m, |vd|<0.1 m/s for 3 s)", quiet_since is not None, f"alt={o.alt:.2f} vd={o.vd:.2f}")
    if o.mode != "HOLD" and not o.armed:
        await o.s.action.hold()
    t, out = hover_task(cmdr, 1.0, 3.0)
    await t
    check("a hover after the reconnect completes", out["res"][0] == 200 and (out["res"][1] or {}).get("status") == "COMPLETED", f"{out['res'][0]}")


async def case_reconnect_settled():
    await case_reconnect(settle=True)


# ---------------------------------------------------------------- rejected commands
async def case_reject():
    o, cmdr, _ = await setup(wait_ready=False)
    pre = await blocking(req, "GET", "/api/preflight-check", token=cmdr)
    d = (pre[1] or {}).get("drones", {}).get("0", {}) if pre[0] == 200 else {}
    note("preflight_at_first_request", {"http": pre[0], "ready": d.get("ready"), "blocking": (pre[1] or {}).get("blocking_reasons")})
    if d and not d.get("ready"):
        s, b = await blocking(req, "POST", "/api/single-drone/hover-sequence", {"target_altitude_m": 1.0, "hover_duration_s": 3.0}, token=cmdr)
        check("hover requested before preflight is green is refused, with a reason", s != 200 and bool(b), f"{s} {json.dumps(b)[:240]}")
        check("and PX4 was not armed by it", o.armed is False, f"armed={o.armed}")
    else:
        note("arm_before_preflight_green", "not exercised: preflight was already green at the first request")
    ok = await blocking(lib.wait_ready, cmdr, 150)
    check("preflight becomes green", ok, brief())
    await o.s.action.land()  # PX4 now in LAND on the ground: the F4 'not armable' case
    await asyncio.sleep(3)
    s, b = await blocking(req, "POST", "/api/single-drone/hover-sequence", {"target_altitude_m": 1.0, "hover_duration_s": 3.0}, token=cmdr)
    note("hover_while_px4_in_LAND_mode", {"http": s, "detail": json.dumps(b)[:300]})
    check("hover is refused while PX4 is in LAND mode, naming the flight mode", s != 200 and "LAND" in json.dumps(b).upper(), f"{s} {json.dumps(b)[:240]}")
    check("and PX4 stays disarmed", o.armed is False, f"armed={o.armed}")
    s, b = await blocking(req, "POST", "/api/drone/0/land", token=cmdr)
    note("land_requested_on_the_ground_disarmed", {"http": s, "body": json.dumps(b)[:300]})
    check("land on the ground gets a definite answer (success or a stated rejection), never a silent one", s in (200, 409, 412, 502) and bool(b), f"{s} {json.dumps(b)[:200]}")
    await o.s.action.hold()
    ok = await blocking(lib.wait_ready, cmdr, 90)
    check("after restoring HOLD the preflight is green and a hover completes", ok, brief())
    if ok:
        t, out = hover_task(cmdr, 1.0, 3.0)
        await t
        check("control hover completes", out["res"][0] == 200, f"{out['res'][0]}")


async def case_cancel():
    o, cmdr, _ = await setup()
    body = json.dumps({"target_altitude_m": 1.0, "hover_duration_s": 10.0}).encode()
    sk = socket.create_connection(("127.0.0.1", 8090))
    sk.sendall(
        b"POST /api/single-drone/hover-sequence HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + cmdr.encode()
        + b"\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    await o.wait_for(lambda: o.alt > 0.7, 60, "airborne")
    await asyncio.sleep(2)
    print(ts(), "CLIENT DISCONNECT mid-hover alt", round(o.alt, 2), flush=True)
    sk.close()
    t_c = time.time()
    await asyncio.sleep(1)
    s2, b2 = await blocking(req, "POST", "/api/single-drone/hover-sequence", {"target_altitude_m": 1.0, "hover_duration_s": 5.0}, token=cmdr)
    check("a second hover right after the disconnect is refused (409, guard held)", s2 == 409, f"{s2} {json.dumps(b2)[:160]}")
    ok = await o.wait_for(lambda: o.armed is False, 90, "disarm")
    note("seconds_from_disconnect_to_disarm", round(time.time() - t_c, 1))
    check("the abandoned sequence still lands and disarms safely", ok and o.landed == "ON_GROUND", f"armed={o.armed} landed={o.landed}")
    t_g = time.time()
    while time.time() - t_g < 8 and brief()["armed"] != 0:
        await asyncio.sleep(0.2)
    note("seconds_for_GCS_armed_to_follow_PX4_disarm", round(time.time() - t_g, 2))
    check("GCS armed count follows PX4's disarm within 8 s", brief()["armed"] == 0, brief())
    t2, out2 = hover_task(cmdr, 1.0, 3.0)
    await t2
    check("guard released afterwards: a new hover request is accepted and completes", out2["res"][0] == 200 and (out2["res"][1] or {}).get("status") == "COMPLETED", f"{out2['res'][0]} {json.dumps(out2['res'][1])[:120]}")
    note("limit", "client disconnect only: the in-process CancelledError path cannot be triggered from outside and is covered by the unit test")


# ---------------------------------------------------------------- dead GCS / stream
async def case_gcs_kill():
    o, cmdr, _ = await setup()
    t, out = hover_task(cmdr, 1.0, 25.0)
    await o.wait_for(lambda: o.alt > 0.7, 60, "airborne")
    await asyncio.sleep(3)
    pid = int(subprocess.run(["pgrep", "-f", "uvicorn tara_gcs.mai[n]"], capture_output=True, text=True).stdout.split()[0])
    servers = subprocess.run(["pgrep", "-af", "mavsdk_serve[r]"], capture_output=True, text=True).stdout
    note("mavsdk_servers_before_kill", servers.splitlines())
    os.kill(pid, signal.SIGKILL)
    t_k = time.time()
    n0 = len(o.events)
    print(ts(), f"GCS SIGKILL mid-hover pid {pid} alt {o.alt:.2f}", flush=True)
    t_off = t_dis = None
    while time.time() - t_k < 60:
        await asyncio.sleep(0.25)
        if t_off is None and o.mode != "OFFBOARD":
            t_off = time.time() - t_k
        if o.armed is False:
            t_dis = time.time() - t_k
            break
    note("seconds_until_PX4_left_OFFBOARD", t_off)
    note("seconds_until_disarmed", t_dis)
    note("mode_events_after_kill", [e[1:] for e in o.events[n0:]])
    left = subprocess.run(["pgrep", "-af", "mavsdk_serve[r]"], capture_output=True, text=True).stdout
    note("mavsdk_servers_after_kill", left.splitlines())
    check("PX4 leaves OFFBOARD within 10 s of the GCS dying (lifeline stops the orphaned server)", t_off is not None and t_off <= 10.0, t_off)
    check("PX4 ends disarmed on the ground within 60 s", t_dis is not None and o.landed == "ON_GROUND", f"disarmed_at={t_dis} landed={o.landed}")


async def case_stream_drop():
    o, cmdr, _ = await setup()
    servers = subprocess.run(["pgrep", "-af", "mavsdk_serve[r]"], capture_output=True, text=True).stdout
    note("mavsdk_servers", servers.splitlines())
    subprocess.run(["pkill", "-9", "-f", "mavsdk_server -p 5005[1]"])
    t_k = time.time()
    print(ts(), "GCS mavsdk_server SIGKILL (stream drop) on the ground", flush=True)
    t0 = t_back = None
    seen = []
    while time.time() - t_k < 60:
        b = brief()
        seen.append((round(time.time() - t_k, 1), b["connected"]))
        if t0 is None and b["connected"] == 0:
            t0 = time.time() - t_k
        if t0 is not None and b["connected"] == 1:
            t_back = time.time() - t_k
            break
        await asyncio.sleep(0.25)
    note("seconds_to_connected_0", t0)
    note("seconds_to_connected_1_again", t_back)
    check("connected shows the drop (0) while the stream is down", t0 is not None, (t0, seen[:6]))
    check("monitors restart and connected returns to 1 within 40 s", t_back is not None and t_back <= 40.0, t_back)
    ok = await blocking(lib.wait_ready, cmdr, 90)
    check("preflight green after the recovery", ok, brief())
    if ok:
        t, out = hover_task(cmdr, 1.0, 3.0)
        await t
        check("a hover after the stream recovery completes", out["res"][0] == 200 and (out["res"][1] or {}).get("status") == "COMPLETED", f"{out['res'][0]}")


async def case_stream_drop_hover():
    o, cmdr, _ = await setup()
    t, out = hover_task(cmdr, 1.0, 25.0)
    await o.wait_for(lambda: o.alt > 0.7, 60, "airborne")
    await asyncio.sleep(3)
    n0 = len(o.events)
    subprocess.run(["pkill", "-9", "-f", "mavsdk_server -p 5005[1]"])
    t_k = time.time()
    print(ts(), "GCS mavsdk_server SIGKILL mid-hover", flush=True)
    t_conn0 = t_off = t_dis = t_back = None
    while time.time() - t_k < 90:
        b = brief()
        if t_conn0 is None and b["connected"] == 0:
            t_conn0 = time.time() - t_k
        if t_back is None and t_conn0 is not None and b["connected"] == 1:
            t_back = time.time() - t_k
        if t_off is None and o.mode != "OFFBOARD":
            t_off = time.time() - t_k
        if o.armed is False:
            t_dis = time.time() - t_k
            break
        await asyncio.sleep(0.25)
    note("seconds_to_connected_0", t_conn0)
    note("seconds_to_PX4_left_OFFBOARD", t_off)
    note("seconds_to_connected_1_again", t_back)
    note("seconds_to_disarm", t_dis)
    note("mode_events_after_drop", [e[1:] for e in o.events[n0:]])
    note("end_state", {"mode": o.mode, "armed": o.armed, "alt": round(o.alt, 2), "landed": o.landed})
    if not t.done():
        try:
            await asyncio.wait_for(t, 90)
        except Exception as e:  # noqa: BLE001
            print("hover task", e)
    s, b = out.get("res", (None, None))
    note("hover_result", {"http": s, "detail": json.dumps(b)[:300]})
    check("hover-sequence fails closed (not 200 COMPLETED)", s is not None and s != 200, f"{s} {json.dumps(b)[:200]}")
    check("connected falls to 0 while the stream is down", t_conn0 is not None, t_conn0)
    check("PX4 is not left hovering in OFFBOARD: leaves OFFBOARD within 15 s", t_off is not None and t_off <= 15.0, t_off)
    check("PX4 ends disarmed on the ground within 90 s", t_dis is not None, f"disarmed_at={t_dis} mode={o.mode} alt={o.alt:.2f}")


# ---------------------------------------------------------------- armed but silent
async def case_armed_silent():
    o, cmdr, _ = await setup()
    await o.s.action.arm()
    await o.wait_for(lambda: o.armed is True, 15, "armed")
    await asyncio.sleep(2)
    b0 = brief()
    check("armed on the ground and fresh: armed 1, armed_stale 0", (b0["armed"], b0["armed_stale"]) == (1, 0), b0)
    pid = px4_pid()
    os.kill(pid, signal.SIGSTOP)
    t_s = time.time()
    print(ts(), "PX4 SIGSTOP while armed on the ground", flush=True)
    t_stale = None
    seen = []
    while time.time() - t_s < 10:
        b = brief()
        seen.append((round(time.time() - t_s, 1), b["armed"], b["armed_stale"], b["connected"]))
        if t_stale is None and b["armed"] == 0 and b["armed_stale"] == 1:
            t_stale = time.time() - t_s
        await asyncio.sleep(0.5)
    mid = brief()
    note("seconds_to_armed_stale_1", t_stale)
    note("status_while_silent", mid)
    check("silent and armed: armed 0 but armed_stale 1 (never reads as disarmed)", mid["armed"] == 0 and mid["armed_stale"] == 1 and mid["armed_stale_ids"] == [0], mid)
    check("it shows within 6 s", t_stale is not None and t_stale <= 6.0, t_stale)
    os.kill(pid, signal.SIGCONT)
    t_c = time.time()
    t_back = None
    while time.time() - t_c < 30:
        b = brief()
        if (b["armed"], b["armed_stale"]) == (1, 0):
            t_back = time.time() - t_c
            break
        await asyncio.sleep(0.25)
    note("seconds_after_SIGCONT_until_armed_1_stale_0", t_back)
    check("after SIGCONT the drone reads armed 1, armed_stale 0 again within 20 s", t_back is not None and t_back <= 20.0, t_back)
    # Right after SIGCONT PX4's estimate is off for several seconds and it reports IN_AIR, in which
    # case it rightly refuses to disarm (first attempt, run `armed-silent`): wait for ON_GROUND.
    settled = await o.wait_for(lambda: o.landed == "ON_GROUND" and abs(o.vd) < 0.1, 40, "ON_GROUND after the resume")
    note("px4_landed_state_settled_after_resume", settled)
    await o.s.action.disarm()
    ok = await o.wait_for(lambda: o.armed is False, 15, "disarm")
    await asyncio.sleep(3)
    b = brief()
    check("disarmed again: armed 0, armed_stale 0", ok and (b["armed"], b["armed_stale"]) == (0, 0), b)


# ---------------------------------------------------------------- second request / upload during a hover
VALID_SHOW = json.dumps({"0": [
    {"time": 0.0, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0},
    {"time": 8.0, "x": 0.0, "y": 0.0, "z": -3.0, "yaw": 0.0}]}).encode()


def multipart(fields):
    b = uuid.uuid4().hex
    parts = []
    for name, (fname, ctype, data) in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{name}"; filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={b}"}


async def case_guard():
    o, cmdr, chor = await setup()
    t, out = hover_task(cmdr, 1.0, 12.0)
    await o.wait_for(lambda: o.alt > 0.7, 60, "airborne")
    await asyncio.sleep(2)
    s, b = await blocking(req, "POST", "/api/single-drone/hover-sequence", {"target_altitude_m": 1.0, "hover_duration_s": 3.0}, token=cmdr)
    check("second hover request during a hover is refused with 409", s == 409, f"{s} {json.dumps(b)[:200]}")
    for who, tok in (("choreographer", chor), ("commander", cmdr)):
        body, hdr = multipart({"file": ("show.json", "application/json", VALID_SHOW)})
        s, b = await blocking(req, "POST", "/api/upload-trajectory", raw=body, headers=hdr, token=tok)
        check(f"upload during a hover ({who}) is refused with 409", s == 409, f"{s} {json.dumps(b)[:200]}")
    await asyncio.wait_for(t, 120)
    check("the original hover still completes", out["res"][0] == 200 and (out["res"][1] or {}).get("status") == "COMPLETED", f"{out['res'][0]}")
    await o.wait_for(lambda: o.armed is False, 30, "disarm")
    body, hdr = multipart({"file": ("show.json", "application/json", VALID_SHOW)})
    s, b = await blocking(req, "POST", "/api/upload-trajectory", raw=body, headers=hdr, token=chor)
    check("control: the same upload after the hover is not refused for the hover (any non-409)", s != 409, f"{s} {json.dumps(b)[:160]}")


async def case_abort_climb():
    o, cmdr, _ = await setup()
    t, out = hover_task(cmdr, 3.0, 10.0)
    await o.wait_for(lambda: o.alt > 0.5, 60, "climbing")
    print(ts(), f"ABORT: POST /api/land mid-climb alt={o.alt:.2f}", flush=True)
    s, b = await blocking(req, "POST", "/api/land", token=cmdr)
    note("land_response", {"http": s, "body": json.dumps(b)[:300]})
    alt_at_abort = o.alt
    note("alt_at_abort", round(alt_at_abort, 2))
    ok = await o.wait_for(lambda: o.armed is False, 60, "disarm")
    check("the vehicle lands and disarms after the abort", ok and o.landed == "ON_GROUND", f"armed={o.armed} landed={o.landed}")
    peak = max((x[1] for x in o.samples), default=0.0)
    check("it never reached the target altitude (peak well under 3 m)", peak < 2.6, f"peak {peak:.2f} m")
    if not t.done():
        await asyncio.wait_for(t, 60)
    s2, b2 = out["res"]
    check("hover-sequence does not report COMPLETED after an abort", not (s2 == 200 and (b2 or {}).get("status") == "COMPLETED"), f"{s2} {json.dumps(b2)[:260]}")
    note("fleet_after", brief())



# ================================================================= rows added for main 1d2d3a4 (includes #240)

async def raw_px4(coro_fn, what):
    """PX4's own verdict on a command sent by the independent client, never through the GCS."""
    try:
        await coro_fn()
        return "ACCEPTED"
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


async def health_of(o):
    try:
        async def one():
            async for h in o.s.telemetry.health():
                return {"local_position_ok": h.is_local_position_ok, "global_position_ok": h.is_global_position_ok,
                        "home_position_ok": h.is_home_position_ok, "armable": h.is_armable}
        return await asyncio.wait_for(one(), 5)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


async def back_to_hold(o):
    try:
        await o.s.action.hold()
    except Exception as e:  # noqa: BLE001
        return f"hold refused: {e}"
    await o.wait_for(lambda: o.mode == "HOLD", 5, "back to HOLD")
    return o.mode


async def land_attempt(o, cmdr, label):
    """One condition, three ways of asking for LAND: PX4 directly, the GCS single-drone route, the GCS fleet-wide route."""
    rec = {"label": label, "health": await health_of(o), "mode_before": o.mode, "armed_before": o.armed}
    rec["px4_direct"] = await raw_px4(o.s.action.land, "land")
    await asyncio.sleep(1.0)
    rec["mode_after_px4_direct"] = o.mode
    rec["restore_1"] = await back_to_hold(o)
    s, b = await blocking(req, "POST", "/api/drone/0/land", token=cmdr)
    rec["gcs_single_drone"] = {"http": s, "body": json.dumps(b)[:500]}
    await asyncio.sleep(1.0)
    rec["mode_after_gcs_single"] = o.mode
    rec["fleet_state_after_single"] = brief().get("fleet_state")
    rec["restore_2"] = await back_to_hold(o)
    s, b = await blocking(req, "POST", "/api/land", token=cmdr)
    rec["gcs_fleet_wide"] = {"http": s, "body": json.dumps(b)[:500]}
    await asyncio.sleep(1.5)
    rec["mode_after_gcs_fleet_wide"] = o.mode
    rec["fleet_state_after_fleet_wide"] = brief().get("fleet_state")
    rec["restore_3"] = await back_to_hold(o)
    rec["denied_by_px4"] = rec["px4_direct"] != "ACCEPTED"
    LADDER.append(rec)
    note(f"attempt: {label}", {k: v for k, v in rec.items() if k not in ("label",)})
    return rec


LADDER = []


def px4_alias(*args, timeout=20):
    """PX4's own shell client inside the container (the same thing an operator on the PX4 console would type)."""
    exe = "/home/px4/PX4-Autopilot/build/px4_sitl_default/bin/px4-" + args[0]
    p = subprocess.run([exe, "--instance", "0", *args[1:]], capture_output=True, text=True, timeout=timeout)
    return {"cmd": " ".join(args), "rc": p.returncode, "out": (p.stdout + p.stderr).strip()[-300:]}


async def case_denied_land():
    """Row: PX4-denied `land`. A ladder of ways to make PX4 refuse LAND; stops at the first refusal and checks what the GCS says."""
    o, cmdr, _ = await setup(True)
    note("ladder_order", ["C1 green+disarmed+ground", "C2 armed on the ground", "C3 baro+gps failure injected",
                            "C4 estimator stopped (px4-ekf2 stop)", "C5 flight termination (kill)"])
    denied = None
    # C1
    r = await land_attempt(o, cmdr, "C1 green, disarmed, on the ground")
    denied = r if r["denied_by_px4"] else None
    # C2
    if not denied:
        a = await raw_px4(o.s.action.arm, "arm")
        note("C2_arm_result", a)
        await o.wait_for(lambda: o.armed is True, 5, "armed")
        r = await land_attempt(o, cmdr, "C2 armed on the ground")
        denied = r if r["denied_by_px4"] else None
        await raw_px4(o.s.action.disarm, "disarm")
        await o.wait_for(lambda: o.armed is False, 15, "disarm")
    # C3
    if not denied:
        note("C3_param", await raw_px4(lambda: o.s.param.set_param_int("SYS_FAILURE_EN", 1), "SYS_FAILURE_EN"))
        from mavsdk.failure import FailureType, FailureUnit
        inj = {}
        for unit in (FailureUnit.SENSOR_BARO, FailureUnit.SENSOR_GPS):
            inj[str(unit)] = await raw_px4(lambda u=unit: o.s.failure.inject(u, FailureType.OFF, 0), "inject")
        note("C3_inject_results", inj)
        await asyncio.sleep(12)
        h = await health_of(o)
        note("C3_health_after_injection", h)
        r = await land_attempt(o, cmdr, "C3 baro + GPS failure injected")
        denied = r if r["denied_by_px4"] else None
    # C4
    if not denied:
        note("C4_ekf2_stop", px4_alias("ekf2", "stop"))
        await asyncio.sleep(6)
        r = await land_attempt(o, cmdr, "C4 estimator stopped")
        denied = r if r["denied_by_px4"] else None
    # C5
    if not denied:
        a = await raw_px4(o.s.action.arm, "arm")
        note("C5_arm_result", a)
        await asyncio.sleep(1)
        note("C5_kill", await raw_px4(o.s.action.kill, "kill"))
        await asyncio.sleep(2)
        r = await land_attempt(o, cmdr, "C5 after flight termination (kill)")
        denied = r if r["denied_by_px4"] else None
    note("tried", [x["label"] for x in LADDER])
    if not denied:
        R["verdict_override"] = "NOT PROVOKED"
        check("PX4 refused LAND under at least one condition", False, "PX4 accepted LAND in every condition tried: " + "; ".join(x["label"] for x in LADDER))
        return
    check("PX4 refused LAND directly (independent client)", True, f"{denied['label']}: {denied['px4_direct']}")
    single = denied["gcs_single_drone"]
    check("GCS single-drone land reports the refusal (not a 200 success)", single["http"] != 200, f"{single['http']} {single['body'][:240]}")
    check("GCS single-drone land: PX4 did not go into LAND", denied["mode_after_gcs_single"] != "LAND", f"mode={denied['mode_after_gcs_single']}")
    fw = denied["gcs_fleet_wide"]
    check("GCS fleet-wide /api/land reports the refusal (not a 200 success)", fw["http"] != 200, f"{fw['http']} {fw['body'][:240]}")
    check("fleet state does not claim LANDING while PX4 refused LAND", denied["fleet_state_after_fleet_wide"] != "LANDING",
          f"fleet_state={denied['fleet_state_after_fleet_wide']} mode={denied['mode_after_gcs_fleet_wide']}")


async def case_cold_start():
    """Row: rejected command before the first green preflight. Started the moment the stack is launched (run_one.sh EARLY=1)."""
    global OBS
    t_launch = time.time()
    o = Observer()
    OBS = o
    obs_task = asyncio.create_task(o.start())
    tok = None
    while tok is None and time.time() - t_launch < 240:
        try:
            tok = lib.login("cmdr", "cmdr passphrase")
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.3)
    note("t_gcs_api_login_s", round(time.time() - t_launch, 1))
    if not tok:
        check("GCS answered within 240 s", False, "no login")
        return
    conn_t = None
    while time.time() - t_launch < 240:
        st = lib.status()
        if st and st.get("connected", 0) >= 1:
            conn_t = time.time() - t_launch
            break
        await asyncio.sleep(0.1)
    note("t_drone_connected_to_gcs_s", round(conn_t, 1) if conn_t else None)
    tl = []
    fired = False
    green_t = None
    first_not_ready = None
    while time.time() - t_launch < 240:
        s, b = await blocking(req, "GET", "/api/preflight-check", token=tok)
        d = (b or {}).get("drones", {}).get("0", {}) if s == 200 else {}
        ready = bool(d.get("ready"))
        tl.append({"t": round(time.time() - t_launch, 2), "http": s, "ready": ready,
                   "blocking": (b or {}).get("blocking_reasons"), "launch_permitted": (b or {}).get("launch_permitted")})
        if ready:
            green_t = time.time() - t_launch
            break
        if not fired and conn_t is not None:
            fired = True
            first_not_ready = tl[-1]
            note("first_not_ready_observation", first_not_ready)
            ev = {}
            ev["observer_connected"] = obs_task.done()
            ev["armed_before"] = o.armed
            s1, b1 = await blocking(req, "POST", "/api/single-drone/hover-sequence", {"target_altitude_m": 1.0, "hover_duration_s": 3.0}, token=tok)
            ev["hover_sequence"] = {"http": s1, "body": json.dumps(b1)[:400]}
            s2, b2 = await blocking(req, "POST", "/api/preflight/latch/request", {}, token=tok)
            ev["latch_request"] = {"http": s2, "body": json.dumps(b2)[:300]}
            s3, b3 = await blocking(req, "POST", "/api/launch", token=tok)
            ev["launch"] = {"http": s3, "body": json.dumps(b3)[:400]}
            ev["fleet_state_after_launch_attempt"] = brief().get("fleet_state")
            s4, b4 = await blocking(req, "POST", "/api/drone/0/land", token=tok)
            ev["gcs_single_drone_land_cold"] = {"http": s4, "body": json.dumps(b4)[:400]}
            if obs_task.done() and not obs_task.cancelled() and o.armed is not None:
                ev["px4_direct_arm"] = await raw_px4(o.s.action.arm, "arm")
                await asyncio.sleep(1.0)
                ev["armed_after_px4_direct_arm"] = o.armed
                ev["px4_direct_land_cold"] = await raw_px4(o.s.action.land, "land")
                if o.armed:
                    await raw_px4(o.s.action.disarm, "disarm")
                    await o.wait_for(lambda: o.armed is False, 15, "disarm")
            else:
                ev["px4_direct_arm"] = "not attempted: observer not yet connected to PX4"
            pre2 = await blocking(req, "GET", "/api/preflight-check", token=tok)
            d2 = (pre2[1] or {}).get("drones", {}).get("0", {}) if pre2[0] == 200 else {}
            ev["preflight_ready_right_after_the_attempts"] = bool(d2.get("ready"))
            ev["status_after"] = brief()
            R["notes"]["cold_start_attempts"] = ev
            print(ts(), "COLD-START ATTEMPTS", json.dumps(ev)[:900], flush=True)
        await asyncio.sleep(0.25)
    R["notes"]["preflight_timeline_tail"] = tl[-12:]
    R["notes"]["preflight_samples"] = len(tl)
    note("t_first_green_s", round(green_t, 1) if green_t else None)
    if not fired:
        R["verdict_override"] = "NOT EXERCISED"
        check("a command was sent while preflight was not green", False, "preflight was already green when the drone first appeared")
        return
    ev = R["notes"]["cold_start_attempts"]
    check("preflight was NOT green when the commands were sent", ev["preflight_ready_right_after_the_attempts"] is False or first_not_ready["ready"] is False,
          f"first_not_ready={first_not_ready}")
    check("hover-sequence before green is refused, with a reason", ev["hover_sequence"]["http"] != 200 and bool(ev["hover_sequence"]["body"]), json.dumps(ev["hover_sequence"])[:260])
    check("launch before green is refused, with a reason", ev["launch"]["http"] != 200 and bool(ev["launch"]["body"]), json.dumps(ev["launch"])[:260])
    check("fleet stayed out of PREARM/ARMED/TAKEOFF after the launch attempt", ev["fleet_state_after_launch_attempt"] in ("STAGED", None, "DISARMED"), f"fleet_state={ev['fleet_state_after_launch_attempt']}")
    check("single-drone land at cold start gets a definite answer", ev["gcs_single_drone_land_cold"]["http"] in (200, 409, 412, 502), json.dumps(ev["gcs_single_drone_land_cold"])[:260])
    pa = ev.get("px4_direct_arm")
    check("PX4 itself refused arm before preflight was green (or the observer could not reach it yet)", pa not in ("ACCEPTED",), f"{pa}; armed_after={ev.get('armed_after_px4_direct_arm')}")
    check("preflight turned green later", green_t is not None, f"t_first_green_s={green_t}")
    if green_t is not None:
        t, out = hover_task(tok, 1.0, 3.0)
        await t
        check("a normal hover completes once preflight is green", out["res"][0] == 200, f"{out['res'][0]}")



async def _denied_land_term(baseline):
    """Row: PX4-denied `land`, provoked the way PX4's source says it can be: NAV_LAND is forced through every mode
    requirement (Commander.cpp, force=true), but UserModeIntention::change never leaves NAVIGATION_STATE_TERMINATION.
    Termination is entered by DO_FLIGHTTERMINATION while armed, so: arm on the pad, terminate, then ask for LAND three ways."""
    o, cmdr, _ = await setup(True)
    note("why_this_provocation", "PX4 v1.16.2 Commander.cpp NAV_LAND uses force=true (mode requirements cannot deny it); "
         "UserModeIntention.cpp: 'never allow to change out of termination state'; DO_FLIGHTTERMINATION is DENIED when disarmed")
    if baseline:
        r0 = await land_attempt(o, cmdr, "baseline: green, disarmed, on the pad")
        check("baseline: PX4 accepts LAND when healthy", not r0["denied_by_px4"], r0["px4_direct"])
    note("fleet_state_before_arm", brief().get("fleet_state"))
    note("arm", await raw_px4(o.s.action.arm, "arm"))
    await o.wait_for(lambda: o.armed is True, 10, "armed")
    check("PX4 armed on the pad before termination", o.armed is True, f"armed={o.armed}")
    note("terminate_result", await raw_px4(o.s.action.terminate, "terminate"))
    await asyncio.sleep(2)
    note("state_after_terminate", {"observer_mode": o.mode, "observer_armed": o.armed, "health": await health_of(o), "gcs": brief()})
    r = await land_attempt(o, cmdr, "after DO_FLIGHTTERMINATION while armed (nav state TERMINATION)")
    if not r["denied_by_px4"]:
        R["verdict_override"] = "NOT PROVOKED"
        check("PX4 refused LAND after termination", False, f"PX4 accepted: {r['px4_direct']}")
        return
    check("PX4 refused LAND directly (independent client)", True, r["px4_direct"])
    single = r["gcs_single_drone"]
    check("GCS single-drone land reports the refusal (not a 200 success)", single["http"] != 200, f"{single['http']} {single['body'][:300]}")
    check("GCS single-drone land: PX4 did not go into LAND", r["mode_after_gcs_single"] != "LAND", f"mode={r['mode_after_gcs_single']}")
    fw = r["gcs_fleet_wide"]
    check("GCS fleet-wide /api/land reports the refusal (not a 200 success)", fw["http"] != 200, f"{fw['http']} {fw['body'][:300]}")
    check("fleet state does not claim LANDING while PX4 refused LAND", r["fleet_state_after_fleet_wide"] != "LANDING",
          f"fleet_state={r['fleet_state_after_fleet_wide']} mode={r['mode_after_gcs_fleet_wide']}")


async def case_denied_land_termination():
    await _denied_land_term(True)


async def case_denied_land_termination_clean():
    """Same provocation without the baseline LAND first, so the fleet state is still STAGED when the fleet-wide call is made."""
    await _denied_land_term(False)



def obs_connected(o):
    return any(e[1] == "connected" for e in o.events)


async def case_cold_start_late_observer():
    """Row: rejected command before the first green preflight (v2). The scenario starts with the stack. At the moment the
    drone first appears in the GCS (preflight red) it sends the GCS-level commands, THEN starts the independent PX4 client and
    tries PX4's own `arm` every 0.3 s until PX4 accepts, so the first PX4 answers are given while PX4 is still not armable."""
    global OBS
    t_launch = time.time()
    tok = None
    while tok is None and time.time() - t_launch < 240:
        try:
            tok = lib.login("cmdr", "cmdr passphrase")
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.3)
    note("t_gcs_api_login_s", round(time.time() - t_launch, 1))
    if not tok:
        check("GCS answered within 240 s", False, "no login")
        return
    conn_t = None
    while time.time() - t_launch < 240:
        st = lib.status()
        if st and st.get("connected", 0) >= 1:
            conn_t = time.time() - t_launch
            break
        await asyncio.sleep(0.1)
    note("t_drone_connected_to_gcs_s", round(conn_t, 1) if conn_t else None)
    tl = []

    async def preflight():
        s, b = await blocking(req, "GET", "/api/preflight-check", token=tok)
        d = (b or {}).get("drones", {}).get("0", {}) if s == 200 else {}
        row = {"t": round(time.time() - t_launch, 2), "http": s, "ready": bool(d.get("ready")),
               "blocking": (b or {}).get("blocking_reasons"), "drone0": {k: d.get(k) for k in ("ready", "reasons", "gates") if k in d}}
        tl.append(row)
        return row

    first = await preflight()
    note("first_preflight_after_connect", first)
    ev = {"t_first_not_ready_s": first["t"], "first_ready": first["ready"]}
    if first["ready"]:
        R["verdict_override"] = "NOT EXERCISED"
        check("preflight was red when the drone first appeared", False, "already green at first look")
        return
    s1, b1 = await blocking(req, "POST", "/api/single-drone/hover-sequence", {"target_altitude_m": 1.0, "hover_duration_s": 3.0}, token=tok)
    ev["gcs_hover_sequence"] = {"http": s1, "body": json.dumps(b1)[:420]}
    s2, b2 = await blocking(req, "POST", "/api/preflight/latch/request", {}, token=tok)
    ev["gcs_latch_request"] = {"http": s2}
    s3, b3 = await blocking(req, "POST", "/api/launch", token=tok)
    ev["gcs_launch"] = {"http": s3, "body": json.dumps(b3)[:420]}
    ev["fleet_state_after_gcs_attempts"] = brief().get("fleet_state")
    # now the independent PX4 client (also the first normal-link GCS heartbeat PX4 sees in this run)
    o = Observer()
    OBS = o
    t_obs = time.time()
    obs_task = asyncio.create_task(o.start())
    ev["observer_started_at_s"] = round(t_obs - t_launch, 1)
    arm_tries = []
    green_t = None
    px4_accept_t = None
    deadline = time.time() + 150
    while time.time() < deadline:
        row = await preflight()
        if row["ready"] and green_t is None:
            green_t = row["t"]
        if obs_connected(o) and px4_accept_t is None:
            h = await health_of(o)
            res = await raw_px4(o.s.action.arm, "arm")
            arm_tries.append({"t": round(time.time() - t_launch, 2), "result": res, "gcs_preflight_ready_at_that_time": row["ready"], "px4_health": h})
            if res == "ACCEPTED":
                px4_accept_t = time.time() - t_launch
                await asyncio.sleep(0.5)
                await raw_px4(o.s.action.disarm, "disarm")
                await o.wait_for(lambda: o.armed is False, 15, "disarm")
        if green_t is not None and (px4_accept_t is not None or time.time() - t_launch > 120):
            break
        await asyncio.sleep(0.3)
    ev["arm_tries_count"] = len(arm_tries)
    ev["arm_tries_first5"] = arm_tries[:5]
    ev["arm_tries_last"] = arm_tries[-1] if arm_tries else None
    ev["observer_events"] = o.events[:6]
    ev["t_px4_arm_accepted_s"] = round(px4_accept_t, 1) if px4_accept_t else None
    ev["t_gcs_preflight_first_green_s"] = green_t
    R["notes"]["cold_start_v2"] = ev
    R["notes"]["preflight_samples"] = len(tl)
    R["notes"]["preflight_timeline_first_and_last"] = tl[:3] + tl[-3:]
    R["notes"]["preflight_red_reasons_seen"] = sorted({r for x in tl if not x["ready"] for r in (x["blocking"] or [])})
    print(ts(), "COLD-START V2", json.dumps(ev)[:1200], flush=True)
    check("preflight was red (GCS) when the GCS-level commands were sent", first["ready"] is False, json.dumps(first)[:240])
    check("hover-sequence before green is refused, with a reason", ev["gcs_hover_sequence"]["http"] != 200 and bool(ev["gcs_hover_sequence"]["body"]), json.dumps(ev["gcs_hover_sequence"])[:300])
    check("launch before green is refused, with a reason", ev["gcs_launch"]["http"] != 200 and bool(ev["gcs_launch"]["body"]), json.dumps(ev["gcs_launch"])[:300])
    check("fleet stayed out of PREARM/ARMED/TAKEOFF after those attempts", ev["fleet_state_after_gcs_attempts"] in ("STAGED", None), f"fleet_state={ev['fleet_state_after_gcs_attempts']}")
    denied_first = bool(arm_tries) and arm_tries[0]["result"] != "ACCEPTED"
    check("PX4's own first `arm` answer was a refusal (PX4 not yet armable), exercised for real", denied_first,
          f"tries={len(arm_tries)} first={json.dumps(arm_tries[0])[:300] if arm_tries else 'observer never connected'}")
    check("PX4 later accepted `arm` (disarmed again at once)", px4_accept_t is not None, f"t={px4_accept_t}")
    check("GCS preflight turned green", green_t is not None, f"t_first_green_s={green_t}")
    if green_t is not None:
        t, out = hover_task(tok, 1.0, 3.0)
        await t
        check("a normal hover completes once preflight is green", out["res"][0] == 200, f"{out['res'][0]}")



async def case_cold_start_px4_probe():
    """Row: rejected command before the first green preflight (v3). Nothing but the GCS and PX4's OWN shell client is used (no MAVSDK
    observer). From the first moment PX4's shell answers, `commander arm` is tried every 0.3 s and every raw answer is kept; the GCS
    preflight is polled from the moment its API answers. PX4 armed => disarm at once and stop probing."""
    t_launch = time.time()
    probes = []
    state = {"stop": False, "px4_armed_t": None}

    async def prober():
        while not state["stop"] and time.time() - t_launch < 200:
            t = round(time.time() - t_launch, 2)
            try:
                r = await blocking(px4_alias, "commander", "arm", timeout=8)
            except Exception as e:  # noqa: BLE001
                r = {"cmd": "commander arm", "rc": None, "out": f"{type(e).__name__}: {e}"}
            probes.append({"t": t, "rc": r["rc"], "out": r["out"][-260:]})
            await asyncio.sleep(0.3)

    ptask = asyncio.create_task(prober())
    tok = None
    while tok is None and time.time() - t_launch < 240:
        try:
            tok = lib.login("cmdr", "cmdr passphrase")
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.3)
    note("t_gcs_api_login_s", round(time.time() - t_launch, 1))
    tl = []
    fired = False
    ev = {}
    green_t = None
    conn_t = None
    while time.time() - t_launch < 200:
        st = lib.status()
        connected = (st or {}).get("connected", 0)
        if connected >= 1 and conn_t is None:
            conn_t = round(time.time() - t_launch, 2)
        s, b = await blocking(req, "GET", "/api/preflight-check", token=tok)
        d = (b or {}).get("drones", {}).get("0", {}) if s == 200 else {}
        row = {"t": round(time.time() - t_launch, 2), "connected": connected, "armed": (st or {}).get("armed"), "ready": bool(d.get("ready")),
               "blocking": (b or {}).get("blocking_reasons")}
        if not tl or tl[-1]["ready"] != row["ready"] or tl[-1]["connected"] != row["connected"] or tl[-1].get("armed") != row["armed"]:
            tl.append(row)
        if connected >= 1 and not row["ready"] and not fired:
            fired = True
            s1, b1 = await blocking(req, "POST", "/api/single-drone/hover-sequence", {"target_altitude_m": 1.0, "hover_duration_s": 3.0}, token=tok)
            ev["gcs_hover_sequence"] = {"t": row["t"], "http": s1, "body": json.dumps(b1)[:420]}
            await blocking(req, "POST", "/api/preflight/latch/request", {}, token=tok)
            s3, b3 = await blocking(req, "POST", "/api/launch", token=tok)
            ev["gcs_launch"] = {"http": s3, "body": json.dumps(b3)[:420]}
            ev["fleet_state_after_gcs_attempts"] = brief().get("fleet_state")
        if row["ready"] and green_t is None:
            green_t = row["t"]
        if (st or {}).get("armed", 0) and state["px4_armed_t"] is None:
            state["px4_armed_t"] = row["t"]
            state["stop"] = True
            await asyncio.sleep(0.5)
            ev["px4_disarm"] = await blocking(px4_alias, "commander", "disarm", timeout=8)
        if green_t is not None and state["px4_armed_t"] is not None and time.time() - t_launch > green_t + 3:
            break
        await asyncio.sleep(0.2)
    state["stop"] = True
    await ptask
    answers = [p_ for p_ in probes if p_["rc"] is not None]
    not_up = [p_ for p_ in probes if p_["rc"] is None or "not" in p_["out"].lower() and "connect" in p_["out"].lower()]
    ev["probe_count"] = len(probes)
    ev["probe_first_6"] = probes[:6]
    ev["probe_distinct_answers"] = sorted({p_["out"] for p_ in probes})[:12]
    ev["probe_last_3"] = probes[-3:]
    ev["t_gcs_connected_s"] = conn_t
    ev["t_gcs_preflight_first_green_s"] = green_t
    ev["t_px4_armed_seen_by_gcs_s"] = state["px4_armed_t"]
    R["notes"]["cold_start_v3"] = ev
    R["notes"]["gcs_timeline_changes"] = tl[:30]
    print(ts(), "COLD-START V3", json.dumps({k: v for k, v in ev.items() if k not in ("probe_first_6", "probe_last_3")})[:1500], flush=True)
    check("GCS preflight was red when the GCS-level commands were sent", fired, f"fired={fired} t_connected={conn_t}")
    if fired:
        check("hover-sequence before green is refused, with a reason", ev["gcs_hover_sequence"]["http"] != 200, json.dumps(ev["gcs_hover_sequence"])[:300])
        check("launch before green is refused, with a reason", ev["gcs_launch"]["http"] != 200, json.dumps(ev["gcs_launch"])[:300])
        check("fleet stayed out of PREARM/ARMED/TAKEOFF after those attempts", ev["fleet_state_after_gcs_attempts"] in ("STAGED", None), f"fleet_state={ev['fleet_state_after_gcs_attempts']}")
    refusals_before_accept = [p_ for p_ in probes if state["px4_armed_t"] is None or p_["t"] < state["px4_armed_t"]]
    check("PX4 itself was asked to arm repeatedly from boot, and its answers were kept", len(probes) >= 5, f"{len(probes)} probes")
    check("PX4 later armed (the probe was finally accepted) and was disarmed again at once", state["px4_armed_t"] is not None, f"t={state['px4_armed_t']}")
    check("GCS preflight turned green", green_t is not None, f"t={green_t}")
    if green_t is not None:
        t, out = hover_task(tok, 1.0, 3.0)
        await t
        check("a normal hover completes once preflight is green", out["res"][0] == 200, f"{out['res'][0]}")


CASES = {k[5:]: v for k, v in list(globals().items()) if k.startswith("case_")}


async def main(name):
    err = None
    try:
        await asyncio.wait_for(CASES[name](), 540)
    except Exception as e:  # noqa: BLE001
        import traceback

        err = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        check("scenario ran to the end without an exception", False, err)
    R["case"] = name
    R["exception"] = err
    R["verdict"] = R.get("verdict_override") or ("PASS" if R["checks"] and all(c["ok"] for c in R["checks"]) and not err else "FAIL")
    os.makedirs(OUT, exist_ok=True)
    if OBS is not None:
        R["observer_events"] = OBS.events
        with open(f"{OUT}/timeline.csv", "w") as f:
            f.write("t_s,alt_m,vel_down_m_s,mode,armed\n")
            for row in OBS.samples:
                f.write(",".join(str(x) for x in row) + "\n")
    json.dump(R, open(f"{OUT}/result.json", "w"), indent=2)
    print(ts(), "VERDICT", R["verdict"], flush=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
    os._exit(0)
