# ruff: noqa: E401,E702,E741  (verbatim record of the script that produced the stored results; not reformatted so it stays what ran)
"""Assemble one run's record: run.json = context + scenario result + measured cadences."""
import gzip, json, statistics, sys

outd, ctx_path = sys.argv[1], sys.argv[2]
ctx = json.load(open(ctx_path))
try:
    res = json.load(open(f"{outd}/result.json"))
except Exception as e:  # noqa: BLE001
    res = {"verdict": "NO RESULT", "checks": [], "exception": f"result.json missing or unreadable: {e}"}

def cadence(path):
    rows = []
    try:
        rows = [json.loads(l) for l in gzip.open(path + ".gz", "rt") if l.strip()]
    except OSError:
        return {}
    out = {}
    for s in ("landed_state", "position_velocity_ned"):
        ts_ = sorted(r["t"] for r in rows if r["s"] == s)
        if len(ts_) < 3:
            out[s] = {"items": len(ts_)}
            continue
        dt = [b - a for a, b in zip(ts_, ts_[1:])]
        q = sorted(dt)
        d = {
            "items": len(ts_), "span_s": round(ts_[-1] - ts_[0], 1), "mean_hz": round(len(dt) / (ts_[-1] - ts_[0]), 2),
            "median_interval_s": round(statistics.median(dt), 4), "p99_interval_s": round(q[int(0.99 * (len(q) - 1))], 4),
            "max_gap_s": round(max(dt), 3), "gaps_over_2_5s": sum(1 for x in dt if x > 2.5),
        }
        if s == "landed_state":
            vals = [r["v"] for r in sorted((r for r in rows if r["s"] == s), key=lambda r: r["t"])]
            longest = cur = 0; last = None
            for v in vals:
                cur = cur + 1 if v == last else 1; last = v; longest = max(longest, cur)
            d["longest_run_of_identical_values"] = longest
        out[s] = d
    return out

rec = {"run_id": ctx["run_id"], "case": ctx["case"], "verdict": res.get("verdict"), "context": ctx,
       "checks": res.get("checks"), "notes": res.get("notes"), "exception": res.get("exception"),
       "cadence": cadence(f"{outd}/cadence.jsonl"), "observer_events": res.get("observer_events")}
json.dump(rec, open(f"{outd}/run.json", "w"), indent=2)
print(rec["run_id"], rec["verdict"], "|", sum(1 for c in (rec["checks"] or []) if c["ok"]), "/", len(rec["checks"] or []), "checks ok")
for c in rec["checks"] or []:
    if not c["ok"]:
        print("  FAILED:", c["name"], "|", c["detail"][:200])
print("  cadence:", json.dumps(rec["cadence"]))
