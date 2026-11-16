# ruff: noqa
# The scenario of this record. Drop it into the #171 rig's scenarios.py (docs/sitl-171-acceptance/rig/) above the line
# `CASES = {...}`; it needs /work/tools on the path (the rig mounts the repo at /work) and the rig's helpers
# (setup, hover_task, brief, check, note, px4_alias, blocking). Run: run_one.sh RUNID ekf_stop_mid_hover tara

async def case_ekf_stop_mid_hover():
    """Row: a fault where the link stays up but a telemetry field freezes. PX4's estimator is stopped (px4-ekf2 stop) in the
    middle of a hover: the vehicle keeps sending its heartbeat, but position/velocity stop updating. PX4's own estimate freezes
    with the fault, so the vehicle's real altitude is read from Gazebo's model pose (the same stream the planned-vs-actual tool uses)."""
    import statistics

    sys.path.insert(0, "/work/tools")
    from sitl_planned_vs_actual import GazeboRecorder

    rec = GazeboRecorder("drone_show_field", 1)
    rec.start()
    got = await blocking(rec.wait_for_all, 40)
    check("Gazebo pose stream is readable", got, f"{len(rec.samples)} samples, errors {rec.errors}")
    o, cmdr, _ = await setup()
    rest_z = statistics.median(s_[2][0][2] for s_ in rec.samples) if rec.samples else 0.0
    note("gazebo_rest_z", round(rest_z, 3))

    def gz_alt():
        return round(rec.samples[-1][2][0][2] - rest_z, 3) if rec.samples and 0 in rec.samples[-1][2] else None

    t, out = hover_task(cmdr, 1.0, 8.0)
    reached = await o.wait_for(lambda: o.alt >= 0.85 and o.mode == "OFFBOARD", 60, "hover altitude reached in OFFBOARD")
    await asyncio.sleep(1.5)  # inside the 8 s dwell, after HOVER_REACHED
    check("in the hover dwell before the fault", reached and not t.done(), f"px4 alt {o.alt:.2f} m, mode {o.mode}, gazebo {gz_alt()} m, hover task done={t.done()}")
    t_fault = time.time()
    note("ekf2_stop", px4_alias("ekf2", "stop"))
    tl = []
    ended_gz = None
    while time.time() - t_fault < 90:
        b = brief()
        row = {"t": round(time.time() - t_fault, 1), "gz_alt_m": gz_alt(), "px4_alt_m": round(o.alt, 2), "px4_mode": o.mode, "px4_armed": o.armed,
               "px4_landed": o.landed, "connected": b["connected"], "armed": b["armed"], "armed_stale": b["armed_stale"],
               "fleet_state": b["fleet_state"], "hover_done": t.done()}
        tl.append(row)
        if ended_gz is None and row["gz_alt_m"] is not None and row["gz_alt_m"] < 0.15:
            ended_gz = row["t"]
        if t.done() and ended_gz is not None and time.time() - t_fault > 20:
            break
        await asyncio.sleep(0.5)
    note("timeline_every_0.5s_first_40", tl[:40])
    note("timeline_last_row", tl[-1] if tl else None)
    if not t.done():
        t.cancel()
    s_, b_ = out.get("res", (None, None))
    note("hover_response", {"http": s_, "body": json.dumps(b_)[:500]})
    check("hover-sequence does not report COMPLETED", s_ is not None and not (s_ == 200 and (b_ or {}).get("status") == "COMPLETED"), f"{s_} {json.dumps(b_)[:200]}")
    if out.get("t1"):
        check("the sequence stopped within 15 s of the fault", out["t1"] - t_fault <= 15, f"{out['t1'] - t_fault:.1f} s")
    first_visible = next((r["t"] for r in tl if r["connected"] == 0 or r["armed_stale"] == 1), None)
    note("gcs_first_shows_the_freeze_s", first_visible)
    check("the GCS shows the freeze (connected 0 or armed_stale 1) within 6 s", first_visible is not None and first_visible <= 6, f"{first_visible}")
    detail = json.dumps(b_)
    check("the failure text does not blame a pilot (nobody took over)", "operator took over" not in detail, detail[:200])
    check("the failure is reported as a lost or stale vehicle, not as a hand-over", any(w in detail.lower() for w in ("disconnect", "stale", "lost", "no telemetry")), detail[:200])
    check("Gazebo shows the vehicle on the ground (< 0.15 m) within 90 s", ended_gz is not None, f"first below 0.15 m at {ended_gz} s, last {tl[-1] if tl else None}")
    note("px4_own_state_at_end", {k: tl[-1][k] for k in ("px4_mode", "px4_armed", "px4_landed", "px4_alt_m")} if tl else None)
    low = [r["gz_alt_m"] for r in tl if r["gz_alt_m"] is not None]
    note("gazebo_alt_min_max_after_fault", [min(low), max(low)] if low else None)
    rec.stop()
