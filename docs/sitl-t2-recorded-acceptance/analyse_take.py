# ruff: noqa
"""One take's results from its preserved GCS black box. usage: analyse_take.py hover|show TAKE_DIR [SHOW.json]"""
import glob, hashlib, json, os, sys, itertools
import numpy as np
REPO = os.environ.get("REPO", ".")
sys.path[:0] = [REPO + "/packages/tara-gcs", REPO + "/packages/tara-core"]
from tara_gcs import blackbox_replay as br
kind, d = sys.argv[1], sys.argv[2].rstrip("/")
files = sorted(glob.glob(d + "/blackbox/*.jsonl.gz") + glob.glob(d + "/*.jsonl.gz"), key=os.path.getmtime)
assert len(files) == 1, f"expected exactly one black-box session in {d}, found {len(files)}"
f = files[0]
s = br.ReplaySession.load(f, "x")
fr = [x for x in s.frames(max_frames=5000, max_samples=2_000_000)["frames"]]
sm = s.summary()
out = {"take": os.path.basename(d), "blackbox": os.path.basename(f), "blackbox_sha256": hashlib.sha256(open(f, "rb").read()).hexdigest()[:16],
       "truncated": sm["truncated"], "geofence_radius_m": sm.get("geofence_radius_m"), "event_kinds": sm["event_kinds"],
       "safety_events": [e["kind"] for e in s.event_list(br.SAFETY_EVENT_KINDS, limit=50)["events"]]}
if kind == "hover":
    fr = [x for x in fr if x["ids"] == [0]]
    t = np.array([x["t_ms"] for x in fr]) / 1000.0; z = np.array([x["z_m"][0] for x in fr]); armed = np.array([(x["flags"][0] >> 1) & 1 for x in fr]); mode = [x["mode"][0] for x in fr]
    a = np.where(armed == 1)[0]; i0, i1 = a[0], a[-1]; zmax = float(z[i0:i1 + 1].max())
    plateau = (z > zmax - 0.1) & (armed == 1); k = np.where(plateau)[0][-1]
    desc = (np.arange(len(z)) > k) & (z < 0.7) & (z > 0.2) & (armed == 1)
    out.update({"peak_altitude_m": round(zmax, 2), "seconds_within_0.1m_of_peak": round(float(t[plateau][-1] - t[plateau][0]), 1),
                "descent_m_s": round(float(-np.polyfit(t[desc], z[desc], 1)[0]), 2), "final_altitude_m": round(float(z[i1]), 2),
                "armed_at_end": bool(armed[-1]), "modes_flown": sorted(set(mode[i0:i1 + 1]))})
    r = json.load(open(d + "/hover_response.json"))
    out["endpoint"] = {"http": r["status"], "status": r["body"].get("status"), "seconds": round(r["seconds"], 1),
                       "steps": [(x["step"], x["status"]) for x in r["body"]["steps"]],
                       "hover_reached_m": next((x["altitude_m"] for x in r["body"]["steps"] if x["step"] == "HOVER_REACHED"), None),
                       "dwell_s": next((x["dwell_s"] for x in r["body"]["steps"] if x["step"] == "HOVER_HOLD_COMPLETE"), None)}
    out["pass"] = bool(out["endpoint"]["status"] == "COMPLETED" and 0.8 <= out["peak_altitude_m"] <= 1.2 and out["final_altitude_m"] < 0.15
                       and not out["armed_at_end"] and not out["safety_events"] and 0.2 <= out["descent_m_s"] <= 1.5 and not out["truncated"])
else:
    show = json.load(open(sys.argv[3])); n = len(show["drones"])
    fr = [x for x in fr if len(x["ids"]) == n]
    t = np.array([x["t_ms"] for x in fr]) / 1000.0; ids = fr[0]["ids"]
    pos = {k: np.array([[x["x_m"][i], x["y_m"][i], x["z_m"][i]] for x in fr]) for i, k in enumerate(ids)}
    tr = {(e["from"], e["to"]): e for e in s.event_list(["fsm_transition"], limit=200)["events"]}
    t_run = tr[("FORMATION_HOLD", "RUNNING")]["t"] / 1000.0; t_done = tr[("RUNNING", "SHOW_COMPLETE")]["t"] / 1000.0
    plan = {x["id"]: (np.array([w["time"] for w in x["waypoints"]]), np.array([[w["x"], w["y"], -w["z"]] for w in x["waypoints"]])) for x in show["drones"]}
    dur = max(p[0][-1] for p in plan.values())
    pl = lambda k, tt: np.stack([np.interp(tt, plan[k][0], plan[k][1][:, i]) for i in range(3)], axis=1)
    best = None
    for lag in np.arange(-3.0, 20.0, 0.1):
        m = (t >= t_run + lag) & (t <= t_run + lag + dur)
        err = np.concatenate([np.linalg.norm(pos[k][m] - pl(k, t[m] - (t_run + lag)), axis=1) for k in ids])
        if best is None or err.mean() < best[1].mean(): best = (lag, err)
    lag, err = best; fly = (t >= t_run) & (t <= t_done)
    sep = min(np.linalg.norm(pos[a][fly] - pos[b][fly], axis=1).min() for a, b in itertools.combinations(ids, 2))
    tt = np.arange(0, dur, 0.1); psep = min(np.linalg.norm(pl(a, tt) - pl(b, tt), axis=1).min() for a, b in itertools.combinations(ids, 2))
    out.update({"drones": n, "lifecycle": [(a, b, round(e["t"] / 1000, 1)) for (a, b), e in sorted(tr.items(), key=lambda kv: kv[1]["t"])],
                "running_s": round(t_done - t_run, 1), "plan_duration_s": round(float(dur), 1), "start_offset_s": round(float(lag), 1),
                "tracking_err_mean_m": round(float(err.mean()), 2), "tracking_err_max_m": round(float(err.max()), 2),
                "planned_min_sep_m": round(float(psep), 2), "flown_min_sep_m": round(float(sep), 2),
                "max_alt_flown_m": round(float(max(pos[k][:, 2].max() for k in ids)), 2), "max_alt_planned_m": round(float(max(p[1][:, 2].max() for p in plan.values())), 2),
                "final_altitude_m": {str(k): round(float(pos[k][-1, 2]), 2) for k in ids}})
    out["pass"] = bool(out["tracking_err_max_m"] < 1.0 and out["flown_min_sep_m"] >= 2.0 and not out["safety_events"] and max(out["final_altitude_m"].values()) < 0.3
                       and ("RUNNING", "SHOW_COMPLETE") in tr and not out["truncated"])
print(json.dumps(out, indent=1))
