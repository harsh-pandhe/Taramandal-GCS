# ruff: noqa
# The scenario of this record. Drop it into the #171 rig's scenarios.py (docs/sitl-171-acceptance/rig/) above the line
# `CASES = {...}`; it needs /work/tools on the path (the rig mounts the repo at /work) and the rig's helpers
# (setup, brief, check, note, px4_alias, raw_px4, blocking, req). Run: run_one.sh RUNID denied_land_airborne_show tara
# (the rig's outer timeout must allow about 400 s: the show is built, flown and then watched for 95 s).

async def case_denied_land_airborne_show():
    """Row: PX4-denied `land` while a show is RUNNING (the airborne half of #294). A signed 1-drone show is flown for real; mid-show PX4's
    estimator is stopped, after which PX4 refuses LAND. The GCS is then asked to land (fleet-wide and single-drone) while it still believes
    the show is running; the checks are the HTTP answers and the fleet state it claims. PX4's own verdict is taken afterwards from the
    independent client, so the denial is attributed to PX4 and not assumed."""
    sys.path.insert(0, "/work/tools")
    from sitl_planned_vs_actual import Api, build_stand_in, multipart

    o, cmdr, _ = await setup()
    gcs = Api("http://127.0.0.1:8090", cmdr)
    studio = Api("http://127.0.0.1:8001")
    EMERG = {"LANDING", "RTL", "ESTOP", "STAGED", "DISARMED"}

    async def fleet_state_is(targets, timeout):
        end = time.time() + timeout
        while time.time() < end:
            st = (await blocking(gcs.call, "GET", "/api/status", timeout=10))[1]
            if isinstance(st, dict) and st.get("fleet_state") in targets | EMERG:
                return st["fleet_state"]
            await asyncio.sleep(0.5)
        return "timeout"

    choreo = {"drones": build_stand_in(1, 10.0)["drones"]}
    note("planned_duration_s", max(d["waypoints"][-1]["time"] for d in choreo["drones"]))
    rep = await blocking(studio.must, "POST", "/api/validate", choreo, timeout=300)
    check("Studio certifies the 1-drone show", bool(rep.get("feasibility_certified")), rep.get("status"))
    exp = await blocking(studio.must, "POST", "/api/export", {"choreo": choreo, "format": "json", "sign": True}, timeout=300)
    body, ctype = multipart({
        "file": (exp["filename"], exp["content"].encode(), "application/json"),
        "signature": (exp["filename"] + ".sig.json", exp["signature"]["content"].encode(), "application/json"),
    })
    up = await blocking(gcs.must, "POST", "/api/upload-trajectory", raw=body, content_type=ctype)
    check("the GCS verifies the show's signature", bool(up.get("signature", {}).get("verified")), up.get("signature"))
    await blocking(gcs.must, "POST", "/api/upload-mission", timeout=300)
    latch = await blocking(gcs.must, "POST", "/api/preflight/latch/request", {})
    await blocking(gcs.must, "POST", "/api/preflight/latch/confirm", {"token": latch["token"]})
    await blocking(gcs.must, "POST", "/api/launch")
    st = await fleet_state_is({"FORMATION_HOLD"}, 150)
    check("the fleet reaches FORMATION_HOLD", st == "FORMATION_HOLD", st)
    await asyncio.sleep(3)
    await blocking(gcs.must, "POST", "/api/start-trajectory", {"mode": "LOCAL", "execution": "streamed"})
    st = await fleet_state_is({"RUNNING"}, 60)
    check("the show is RUNNING", st == "RUNNING", st)
    await asyncio.sleep(15)
    check("airborne in the show before the fault", o.alt > 2.0 and brief()["fleet_state"] == "RUNNING", f"px4 alt {o.alt:.1f} m, {brief()}")

    t_fault = time.time()
    note("ekf2_stop", px4_alias("ekf2", "stop"))
    await asyncio.sleep(0.3)
    before = brief()["fleet_state"]
    s1, b1 = await blocking(req, "POST", "/api/land", token=cmdr)
    after1 = brief()["fleet_state"]
    note("fleet_wide_land", {"t_after_fault_s": round(time.time() - t_fault, 2), "http": s1, "body": json.dumps(b1)[:600], "state_before": before, "state_right_after": after1})
    await asyncio.sleep(1.0)
    after2 = brief()["fleet_state"]
    s2, b2 = await blocking(req, "POST", "/api/drone/0/land", token=cmdr)
    note("single_drone_land", {"http": s2, "body": json.dumps(b2)[:600], "state_after": brief()["fleet_state"]})
    px4_answer = await raw_px4(o.s.action.land, "land")
    note("px4_direct_land_answer", px4_answer)
    tl = []
    for _ in range(95):
        b = brief()
        tl.append({"t": round(time.time() - t_fault, 1), "fleet_state": b["fleet_state"], "connected": b["connected"], "armed": b["armed"], "armed_stale": b["armed_stale"]})
        await asyncio.sleep(1.0)
    note("fleet_timeline_first_10", tl[:10])
    chg = [tl[0]] + [tl[i] for i in range(1, len(tl)) if tl[i]["fleet_state"] != tl[i - 1]["fleet_state"]]
    note("fleet_state_changes_in_95s", chg)
    note("fleet_timeline_last", tl[-1])
    s3, fs = await blocking(req, "GET", "/api/fleet-state", token=cmdr)
    note("fleet_history_tail", (fs or {}).get("history", [])[-10:] if isinstance(fs, dict) else fs)

    check("PX4 itself refuses land with the estimator stopped (independent client)", px4_answer != "ACCEPTED" and "DENIED" in px4_answer.upper(), px4_answer[:160])
    check("GCS fleet-wide land does not answer 200 success", s1 != 200, f"{s1} {json.dumps(b1)[:240]}")
    check("the refusal names PX4's denial", "denied" in json.dumps(b1).lower(), json.dumps(b1)[:240])
    check("the fleet is not claimed to be LANDING right after the refused fleet-wide land", after1 != "LANDING", f"{before} -> {after1}")
    check("nor one second later", after2 != "LANDING", after2)
    check("GCS single-drone land does not answer 200 success", s2 != 200, f"{s2} {json.dumps(b2)[:240]}")
