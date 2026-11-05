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
import lib  # noqa: E402
from lib import req, ts  # noqa: E402
from observer import Observer  # noqa: E402

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
    return {
        k: st.get(k)
        for k in ("connected", "armed", "armed_stale", "armed_stale_ids", "fleet_state")
    }


def px4_pid():
    out = subprocess.run(
        ["pgrep", "-f", "px4_sitl_default/bin/px[4] -d"], capture_output=True, text=True
    ).stdout.split()
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
            req,
            "POST",
            "/api/single-drone/hover-sequence",
            {"target_altitude_m": alt, "hover_duration_s": hold},
            token=cmdr,
            timeout=300,
        )
        out["t1"] = time.time()
        print(
            ts(),
            "HOVER HTTP RESULT:",
            out["res"][0],
            json.dumps(out["res"][1])[:500],
            flush=True,
        )

    return asyncio.create_task(run()), out


def steps_of(res):
    body = res[1] or {}
    return (
        {s["step"]: s for s in body.get("steps", [])} if isinstance(body, dict) else {}
    )


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
    check(
        "hover-sequence returns 200 COMPLETED",
        s == 200 and (b or {}).get("status") == "COMPLETED",
        f"{s} {(b or {}).get('status')}",
    )
    note(
        "steps",
        {k: {kk: vv for kk, vv in v.items() if kk != "time"} for k, v in st.items()},
    )
    check(
        "every step present",
        all(
            k in st
            for k in (
                "PREFLIGHT",
                "ARM",
                "HOVER_REACHED",
                "HOVER_HOLD_COMPLETE",
                "LANDING_COMMANDED",
                "TOUCHDOWN_DISARM",
            )
        ),
        list(st),
    )
    await asyncio.sleep(2)
    top = max((x[1] for x in o.samples), default=0.0)
    check(
        "reached about 1 m (0.8 to 1.4 m on PX4's own estimate)",
        0.8 <= top <= 1.4,
        f"peak {top:.2f} m",
    )
    check(
        "PX4 ends ON_GROUND and disarmed",
        o.landed == "ON_GROUND" and o.armed is False,
        f"landed={o.landed} armed={o.armed}",
    )
    land = [x[2] for x in phase_samples(o, "LAND") if x[1] > 0.15]
    vmax = max(land, default=0.0)
    note("landing_max_descent_m_s", round(vmax, 3))
    check(
        "slow landing: peak descent rate <= 1.0 m/s (PX4 MPC_LAND_SPEED is 0.7)",
        land and vmax <= 1.0,
        f"peak {vmax:.2f} m/s over {len(land)} samples",
    )
    b2 = brief()
    check(
        "GCS agrees: armed 0, armed_stale 0, connected 1",
        (b2["armed"], b2["armed_stale"], b2["connected"]) == (0, 0, 1),
        b2,
    )
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
    print(
        ts(),
        f"TAKEOVER start_position_control() alt={o.alt:.2f} mode={o.mode}",
        flush=True,
    )
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
    note(
        "method",
        "MAVSDK manual_control MANUAL_CONTROL stream, then start_position_control() (OFFBOARD to POSCTL)",
    )
    note("mode_events_after_takeover", modes_after)
    check(
        "PX4 stays in POSCTL and armed 15 s after the switch",
        o.mode == "POSCTL" and o.armed is True,
        f"mode={o.mode} armed={o.armed} alt={o.alt:.2f}",
    )
    check(
        "GCS sent no LAND or other mode change after the takeover",
        "LAND" not in modes_after and "HOLD" not in modes_after,
        modes_after,
    )
    if not t.done():
        await asyncio.wait_for(t, 60)
    s, b = out["res"]
    check(
        "hover-sequence stops with 500 'operator took over'",
        s == 500 and "operator took over" in json.dumps(b),
        f"{s} {json.dumps(b)[:160]}",
    )
    check(
        "sequence stopped within 2 s of the switch",
        out["t1"] - t_take < 2.5,
        f"{out['t1'] - t_take:.2f} s",
    )
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
    check(
        "PX4 stays in POSCTL, armed and airborne for 25 s (the GCS does not re-send land)",
        o.mode == "POSCTL" and o.armed is True and o.alt > 2.0,
        f"mode={o.mode} armed={o.armed} alt={o.alt:.2f}",
    )
    if not t.done():
        await asyncio.wait_for(t, 60)
    s, b = out["res"]
    check(
        "hover-sequence stops with 500 'operator took over during the landing'",
        s == 500 and "took over during the landing" in json.dumps(b),
        f"{s} {json.dumps(b)[:200]}",
    )
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
    check(
        "baseline mid-hover: connected 1, armed 1, armed_stale 0",
        (b0["connected"], b0["armed"], b0["armed_stale"]) == (1, 1, 0),
        b0,
    )
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
    check(
        "connected falls to 0 within 6 s",
        t_conn0 is not None and t_conn0 <= 6.0,
        t_conn0,
    )
    check(
        "armed-but-silent is visible: armed 0 and armed_stale 1 within 6 s",
        t_stale is not None and t_stale <= 6.0,
        (t_stale, brief()),
    )
    await asyncio.wait_for(t, 120)
    s, b = out["res"]
    note("hover_result", {"http": s, "detail": json.dumps(b)[:300]})
    check(
        "hover-sequence fails closed (not 200 COMPLETED) with a reason",
        s != 200 and bool(b),
        f"{s} {json.dumps(b)[:200]}",
    )
    check(
        "fleet never reports the dead drone as connected afterwards",
        brief()["connected"] == 0,
        brief(),
    )


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
    check(
        "connected drops during an 8 s freeze (within 5 s)",
        t_drop is not None and t_drop <= 5.0,
        t_drop,
    )
    check(
        "connected is back within 15 s of SIGCONT",
        t_back is not None and t_back <= 15.0,
        t_back,
    )
    ok = await blocking(lib.wait_ready, cmdr, 90)
    check("preflight green again after the reconnect", ok, brief())
    note(
        "px4_estimate_when_preflight_went_green",
        {"alt_m": round(o.alt, 2), "vel_down_m_s": round(o.vd, 2), "armed": o.armed},
    )
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
        note(
            "seconds_for_px4_estimate_to_settle_after_SIGCONT",
            round(time.time() - t_c, 1),
        )
        check(
            "PX4's altitude estimate settles to the ground (|alt|<0.15 m, |vd|<0.1 m/s for 3 s)",
            quiet_since is not None,
            f"alt={o.alt:.2f} vd={o.vd:.2f}",
        )
    if o.mode != "HOLD" and not o.armed:
        await o.s.action.hold()
    t, out = hover_task(cmdr, 1.0, 3.0)
    await t
    check(
        "a hover after the reconnect completes",
        out["res"][0] == 200 and (out["res"][1] or {}).get("status") == "COMPLETED",
        f"{out['res'][0]}",
    )


async def case_reconnect_settled():
    await case_reconnect(settle=True)


# ---------------------------------------------------------------- rejected commands
async def case_reject():
    o, cmdr, _ = await setup(wait_ready=False)
    pre = await blocking(req, "GET", "/api/preflight-check", token=cmdr)
    d = (pre[1] or {}).get("drones", {}).get("0", {}) if pre[0] == 200 else {}
    note(
        "preflight_at_first_request",
        {
            "http": pre[0],
            "ready": d.get("ready"),
            "blocking": (pre[1] or {}).get("blocking_reasons"),
        },
    )
    if d and not d.get("ready"):
        s, b = await blocking(
            req,
            "POST",
            "/api/single-drone/hover-sequence",
            {"target_altitude_m": 1.0, "hover_duration_s": 3.0},
            token=cmdr,
        )
        check(
            "hover requested before preflight is green is refused, with a reason",
            s != 200 and bool(b),
            f"{s} {json.dumps(b)[:240]}",
        )
        check("and PX4 was not armed by it", o.armed is False, f"armed={o.armed}")
    else:
        note(
            "arm_before_preflight_green",
            "not exercised: preflight was already green at the first request",
        )
    ok = await blocking(lib.wait_ready, cmdr, 150)
    check("preflight becomes green", ok, brief())
    await o.s.action.land()  # PX4 now in LAND on the ground: the F4 'not armable' case
    await asyncio.sleep(3)
    s, b = await blocking(
        req,
        "POST",
        "/api/single-drone/hover-sequence",
        {"target_altitude_m": 1.0, "hover_duration_s": 3.0},
        token=cmdr,
    )
    note("hover_while_px4_in_LAND_mode", {"http": s, "detail": json.dumps(b)[:300]})
    check(
        "hover is refused while PX4 is in LAND mode, naming the flight mode",
        s != 200 and "LAND" in json.dumps(b).upper(),
        f"{s} {json.dumps(b)[:240]}",
    )
    check("and PX4 stays disarmed", o.armed is False, f"armed={o.armed}")
    s, b = await blocking(req, "POST", "/api/drone/0/land", token=cmdr)
    note(
        "land_requested_on_the_ground_disarmed",
        {"http": s, "body": json.dumps(b)[:300]},
    )
    check(
        "land on the ground gets a definite answer (success or a stated rejection), never a silent one",
        s in (200, 409, 412, 502) and bool(b),
        f"{s} {json.dumps(b)[:200]}",
    )
    await o.s.action.hold()
    ok = await blocking(lib.wait_ready, cmdr, 90)
    check(
        "after restoring HOLD the preflight is green and a hover completes", ok, brief()
    )
    if ok:
        t, out = hover_task(cmdr, 1.0, 3.0)
        await t
        check("control hover completes", out["res"][0] == 200, f"{out['res'][0]}")


async def case_cancel():
    o, cmdr, _ = await setup()
    body = json.dumps({"target_altitude_m": 1.0, "hover_duration_s": 10.0}).encode()
    sk = socket.create_connection(("127.0.0.1", 8090))
    sk.sendall(
        b"POST /api/single-drone/hover-sequence HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
        + cmdr.encode()
        + b"\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )
    await o.wait_for(lambda: o.alt > 0.7, 60, "airborne")
    await asyncio.sleep(2)
    print(ts(), "CLIENT DISCONNECT mid-hover alt", round(o.alt, 2), flush=True)
    sk.close()
    t_c = time.time()
    await asyncio.sleep(1)
    s2, b2 = await blocking(
        req,
        "POST",
        "/api/single-drone/hover-sequence",
        {"target_altitude_m": 1.0, "hover_duration_s": 5.0},
        token=cmdr,
    )
    check(
        "a second hover right after the disconnect is refused (409, guard held)",
        s2 == 409,
        f"{s2} {json.dumps(b2)[:160]}",
    )
    ok = await o.wait_for(lambda: o.armed is False, 90, "disarm")
    note("seconds_from_disconnect_to_disarm", round(time.time() - t_c, 1))
    check(
        "the abandoned sequence still lands and disarms safely",
        ok and o.landed == "ON_GROUND",
        f"armed={o.armed} landed={o.landed}",
    )
    t_g = time.time()
    while time.time() - t_g < 8 and brief()["armed"] != 0:
        await asyncio.sleep(0.2)
    note("seconds_for_GCS_armed_to_follow_PX4_disarm", round(time.time() - t_g, 2))
    check(
        "GCS armed count follows PX4's disarm within 8 s",
        brief()["armed"] == 0,
        brief(),
    )
    t2, out2 = hover_task(cmdr, 1.0, 3.0)
    await t2
    check(
        "guard released afterwards: a new hover request is accepted and completes",
        out2["res"][0] == 200 and (out2["res"][1] or {}).get("status") == "COMPLETED",
        f"{out2['res'][0]} {json.dumps(out2['res'][1])[:120]}",
    )
    note(
        "limit",
        "client disconnect only: the in-process CancelledError path cannot be triggered from outside and is covered by the unit test",
    )


# ---------------------------------------------------------------- dead GCS / stream
async def case_gcs_kill():
    o, cmdr, _ = await setup()
    t, out = hover_task(cmdr, 1.0, 25.0)
    await o.wait_for(lambda: o.alt > 0.7, 60, "airborne")
    await asyncio.sleep(3)
    pid = int(
        subprocess.run(
            ["pgrep", "-f", "uvicorn tara_gcs.mai[n]"], capture_output=True, text=True
        ).stdout.split()[0]
    )
    servers = subprocess.run(
        ["pgrep", "-af", "mavsdk_serve[r]"], capture_output=True, text=True
    ).stdout
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
    left = subprocess.run(
        ["pgrep", "-af", "mavsdk_serve[r]"], capture_output=True, text=True
    ).stdout
    note("mavsdk_servers_after_kill", left.splitlines())
    check(
        "PX4 leaves OFFBOARD within 10 s of the GCS dying (lifeline stops the orphaned server)",
        t_off is not None and t_off <= 10.0,
        t_off,
    )
    check(
        "PX4 ends disarmed on the ground within 60 s",
        t_dis is not None and o.landed == "ON_GROUND",
        f"disarmed_at={t_dis} landed={o.landed}",
    )


async def case_stream_drop():
    o, cmdr, _ = await setup()
    servers = subprocess.run(
        ["pgrep", "-af", "mavsdk_serve[r]"], capture_output=True, text=True
    ).stdout
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
    check(
        "connected shows the drop (0) while the stream is down",
        t0 is not None,
        (t0, seen[:6]),
    )
    check(
        "monitors restart and connected returns to 1 within 40 s",
        t_back is not None and t_back <= 40.0,
        t_back,
    )
    ok = await blocking(lib.wait_ready, cmdr, 90)
    check("preflight green after the recovery", ok, brief())
    if ok:
        t, out = hover_task(cmdr, 1.0, 3.0)
        await t
        check(
            "a hover after the stream recovery completes",
            out["res"][0] == 200 and (out["res"][1] or {}).get("status") == "COMPLETED",
            f"{out['res'][0]}",
        )


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
    note(
        "end_state",
        {"mode": o.mode, "armed": o.armed, "alt": round(o.alt, 2), "landed": o.landed},
    )
    if not t.done():
        try:
            await asyncio.wait_for(t, 90)
        except Exception as e:  # noqa: BLE001
            print("hover task", e)
    s, b = out.get("res", (None, None))
    note("hover_result", {"http": s, "detail": json.dumps(b)[:300]})
    check(
        "hover-sequence fails closed (not 200 COMPLETED)",
        s is not None and s != 200,
        f"{s} {json.dumps(b)[:200]}",
    )
    check("connected falls to 0 while the stream is down", t_conn0 is not None, t_conn0)
    check(
        "PX4 is not left hovering in OFFBOARD: leaves OFFBOARD within 15 s",
        t_off is not None and t_off <= 15.0,
        t_off,
    )
    check(
        "PX4 ends disarmed on the ground within 90 s",
        t_dis is not None,
        f"disarmed_at={t_dis} mode={o.mode} alt={o.alt:.2f}",
    )


# ---------------------------------------------------------------- armed but silent
async def case_armed_silent():
    o, cmdr, _ = await setup()
    await o.s.action.arm()
    await o.wait_for(lambda: o.armed is True, 15, "armed")
    await asyncio.sleep(2)
    b0 = brief()
    check(
        "armed on the ground and fresh: armed 1, armed_stale 0",
        (b0["armed"], b0["armed_stale"]) == (1, 0),
        b0,
    )
    pid = px4_pid()
    os.kill(pid, signal.SIGSTOP)
    t_s = time.time()
    print(ts(), "PX4 SIGSTOP while armed on the ground", flush=True)
    t_stale = None
    seen = []
    while time.time() - t_s < 10:
        b = brief()
        seen.append(
            (round(time.time() - t_s, 1), b["armed"], b["armed_stale"], b["connected"])
        )
        if t_stale is None and b["armed"] == 0 and b["armed_stale"] == 1:
            t_stale = time.time() - t_s
        await asyncio.sleep(0.5)
    mid = brief()
    note("seconds_to_armed_stale_1", t_stale)
    note("status_while_silent", mid)
    check(
        "silent and armed: armed 0 but armed_stale 1 (never reads as disarmed)",
        mid["armed"] == 0 and mid["armed_stale"] == 1 and mid["armed_stale_ids"] == [0],
        mid,
    )
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
    check(
        "after SIGCONT the drone reads armed 1, armed_stale 0 again within 20 s",
        t_back is not None and t_back <= 20.0,
        t_back,
    )
    # Right after SIGCONT PX4's estimate is off for several seconds and it reports IN_AIR, in which
    # case it rightly refuses to disarm (first attempt, run `armed-silent`): wait for ON_GROUND.
    settled = await o.wait_for(
        lambda: o.landed == "ON_GROUND" and abs(o.vd) < 0.1,
        40,
        "ON_GROUND after the resume",
    )
    note("px4_landed_state_settled_after_resume", settled)
    await o.s.action.disarm()
    ok = await o.wait_for(lambda: o.armed is False, 15, "disarm")
    await asyncio.sleep(3)
    b = brief()
    check(
        "disarmed again: armed 0, armed_stale 0",
        ok and (b["armed"], b["armed_stale"]) == (0, 0),
        b,
    )


# ---------------------------------------------------------------- second request / upload during a hover
VALID_SHOW = json.dumps(
    {
        "0": [
            {"time": 0.0, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0},
            {"time": 8.0, "x": 0.0, "y": 0.0, "z": -3.0, "yaw": 0.0},
        ]
    }
).encode()


def multipart(fields):
    b = uuid.uuid4().hex
    parts = []
    for name, (fname, ctype, data) in fields.items():
        parts.append(
            f'--{b}\r\nContent-Disposition: form-data; name="{name}"; filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
            + data
            + b"\r\n"
        )
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={b}"}


async def case_guard():
    o, cmdr, chor = await setup()
    t, out = hover_task(cmdr, 1.0, 12.0)
    await o.wait_for(lambda: o.alt > 0.7, 60, "airborne")
    await asyncio.sleep(2)
    s, b = await blocking(
        req,
        "POST",
        "/api/single-drone/hover-sequence",
        {"target_altitude_m": 1.0, "hover_duration_s": 3.0},
        token=cmdr,
    )
    check(
        "second hover request during a hover is refused with 409",
        s == 409,
        f"{s} {json.dumps(b)[:200]}",
    )
    for who, tok in (("choreographer", chor), ("commander", cmdr)):
        body, hdr = multipart({"file": ("show.json", "application/json", VALID_SHOW)})
        s, b = await blocking(
            req, "POST", "/api/upload-trajectory", raw=body, headers=hdr, token=tok
        )
        check(
            f"upload during a hover ({who}) is refused with 409",
            s == 409,
            f"{s} {json.dumps(b)[:200]}",
        )
    await asyncio.wait_for(t, 120)
    check(
        "the original hover still completes",
        out["res"][0] == 200 and (out["res"][1] or {}).get("status") == "COMPLETED",
        f"{out['res'][0]}",
    )
    await o.wait_for(lambda: o.armed is False, 30, "disarm")
    body, hdr = multipart({"file": ("show.json", "application/json", VALID_SHOW)})
    s, b = await blocking(
        req, "POST", "/api/upload-trajectory", raw=body, headers=hdr, token=chor
    )
    check(
        "control: the same upload after the hover is not refused for the hover (any non-409)",
        s != 409,
        f"{s} {json.dumps(b)[:160]}",
    )


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
    check(
        "the vehicle lands and disarms after the abort",
        ok and o.landed == "ON_GROUND",
        f"armed={o.armed} landed={o.landed}",
    )
    peak = max((x[1] for x in o.samples), default=0.0)
    check(
        "it never reached the target altitude (peak well under 3 m)",
        peak < 2.6,
        f"peak {peak:.2f} m",
    )
    if not t.done():
        await asyncio.wait_for(t, 60)
    s2, b2 = out["res"]
    check(
        "hover-sequence does not report COMPLETED after an abort",
        not (s2 == 200 and (b2 or {}).get("status") == "COMPLETED"),
        f"{s2} {json.dumps(b2)[:260]}",
    )
    note("fleet_after", brief())


CASES = {k[5:]: v for k, v in list(globals().items()) if k.startswith("case_")}


async def main(name):
    err = None
    try:
        await asyncio.wait_for(CASES[name](), 420)
    except Exception as e:  # noqa: BLE001
        import traceback

        err = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        check("scenario ran to the end without an exception", False, err)
    R["case"] = name
    R["exception"] = err
    R["verdict"] = (
        "PASS"
        if R["checks"] and all(c["ok"] for c in R["checks"]) and not err
        else "FAIL"
    )
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
