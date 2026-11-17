# ruff: noqa
#!/usr/bin/env python3
"""Acceptance sequence for the serial transport (#67) against the running rig. Usage: serial_drive.py RUN MODE   (MODE: udp | serial)"""
import json, os, signal, statistics, subprocess, sys, threading, time, urllib.error, urllib.request
RUN, MODE = sys.argv[1], sys.argv[2]
HOME = os.path.expanduser("~")
OUT = f"{HOME}/tara-t2/serial_runs/{RUN}"
REPO = f"{HOME}/Desktop/tara-wt-serial"
GCS = "http://127.0.0.1:8090"
R = {"run": RUN, "mode": MODE, "phases": {}}

def call(method, path, body=None, token=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GCS + path, data=data, method=method, headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null"), time.time() - t
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read()), time.time() - t
        except Exception: return e.code, None, time.time() - t
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None, time.time() - t  # the GCS is not (yet) answering

def status(): return call("GET", "/api/status", timeout=10)[1] or {}
def wait(pred, timeout, step=0.25):
    t = time.time()
    while time.time() - t < timeout:
        v = pred()
        if v: return time.time() - t
        time.sleep(step)
    return None
def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)

tok = None
def ready_pre(): 
    s, b, _ = call("GET", "/api/preflight-check", token=tok, timeout=15)
    return s == 200 and (b or {}).get("drones", {}).get("0", {}).get("ready")
def login():
    global tok
    for _ in range(120):
        s, b, _ = call("POST", "/api/auth/login", {"operator": "cmdr", "passphrase": "cmdr passphrase"}, timeout=10)
        if s == 200: tok = b["token"]; return
        time.sleep(1)
    raise SystemExit("GCS never answered the login")

login()
t = wait(lambda: status().get("connected") == 1, 180); R["connected_after_s"] = t; log("connected after", t)
RATE = os.environ.get("PX4_LINK_RATE")
if RATE:
    # Like MAV_n_RATE on a real autopilot's serial port: PX4's own instance for this link, restarted with a rate cap that fits the line.
    pxm = f"{HOME}/PX4-Autopilot/build/px4_sitl_default/bin/px4-mavlink"
    a = subprocess.run([pxm, "--instance", "0", "stop", "-u", "14580"], capture_output=True, text=True, timeout=30)
    STREAMS = os.environ.get("PX4_LINK_STREAMS")  # "NAME:Hz,NAME:Hz": an explicit per-message set, like MAV_n_* / `mavlink stream` on a real autopilot
    b = subprocess.run([pxm, "--instance", "0", "start", "-x", "-u", "14580", "-r", RATE, "-f", "-m", "custom" if STREAMS else "onboard", "-o", "14540"], capture_output=True, text=True, timeout=30)
    if STREAMS:
        R["px4_link_streams"] = STREAMS
        for item in STREAMS.split(","):
            name, hz = item.split(":")
            subprocess.run([pxm, "--instance", "0", "stream", "-u", "14580", "-s", name, "-r", hz], capture_output=True, text=True, timeout=30)
    R["px4_link_rate_bytes_per_s"] = int(RATE); R["px4_link_restart"] = {"stop": (a.stdout + a.stderr).strip()[-160:], "start": (b.stdout + b.stderr).strip()[-160:]}
    log("px4 onboard link restarted at", RATE, "B/s:", R["px4_link_restart"])
    time.sleep(2)
    R["reconnected_after_rate_cap_s"] = wait(lambda: status().get("connected") == 1, 60); log("connected again after", R["reconnected_after_rate_cap_s"])
R["preflight_green_after_s"] = wait(ready_pre, 180); log("preflight green after", R["preflight_green_after_s"])

# control: a GCS-only call, for the cost of the HTTP path itself
ctl = [call("GET", "/api/status", timeout=10)[2] for _ in range(30)]
R["http_status_ms"] = {"p50": round(statistics.median(ctl) * 1000, 1), "max": round(max(ctl) * 1000, 1)}

# C. a signed 1-drone Studio show, flown through the GCS, scored against Gazebo by the repo's own tool
t0 = time.time()
env = {**os.environ, "MAMBA_ROOT_PREFIX": f"{HOME}/tara-t2/root", "TARA_PASSPHRASE": "cmdr passphrase", "PYTHONPATH": f"{REPO}/packages/tara-gcs:{REPO}/packages/tara-core"}
p = subprocess.run([f"{HOME}/tara-t2/bin/micromamba", "run", "-n", "t2", "python", f"{REPO}/tools/sitl_planned_vs_actual.py", "run", "--drones", "1", "--operator", "cmdr",
                    "--out", f"{OUT}/show", "--label", RUN, "--commit", "serial-verification"], cwd=REPO, env=env, capture_output=True, text=True, timeout=900)
t1 = time.time(); R["phases"]["show"] = {"t0": t0, "t1": t1}
try:
    sj = json.load(open(f"{OUT}/show/{RUN}.json"))
    R["show"] = {"verdict": sj.get("verdict"), "error": sj.get("error"), "playback_s": sj.get("actual", {}).get("playback_duration_s"),
                 "planned_s": sj.get("planned", {}).get("duration_s"), "gazebo": {k: v for k, v in (sj.get("actual", {}).get("gazebo") or {}).items() if k in ("min_separation_m", "max_tracking_error_m", "rtf", "real_time_factor")},
                 "end_state": sj.get("actual", {}).get("end_state"), "upload_mission": sj.get("steps", {}).get("upload_mission")}
except Exception as e:
    R["show"] = {"error": f"no result json: {e}", "stderr_tail": p.stderr[-600:], "stdout_tail": p.stdout[-600:]}
log("show:", R["show"].get("verdict"), R["show"].get("error"))
wait(lambda: status().get("armed") == 0 and status().get("fleet_state") in ("DISARMED", "STAGED"), 120); time.sleep(3)

# B. hover
wait(ready_pre, 120)
t0 = time.time(); s, b, dt = call("POST", "/api/single-drone/hover-sequence", {"target_altitude_m": 1.0, "hover_duration_s": 5.0}, tok)
R["phases"]["hover"] = {"t0": t0, "t1": time.time()}; R["hover"] = {"http": s, "status": (b or {}).get("status"), "seconds": round(dt, 1)}; log("hover:", R["hover"])
wait(lambda: status().get("armed") == 0, 60); time.sleep(3)

# R. RTL in the middle of a hover
wait(ready_pre, 120)
res = {}
th = threading.Thread(target=lambda: res.update(zip(("s", "b", "dt"), call("POST", "/api/single-drone/hover-sequence", {"target_altitude_m": 1.5, "hover_duration_s": 20.0}, tok))))
t0 = time.time(); th.start()
wait(lambda: status().get("fleet_state") in ("STAGED", "DISARMED") and (status().get("armed") or 0) >= 1, 60); time.sleep(9)
s, b, dt = call("POST", "/api/rtl", None, tok); R["rtl"] = {"http": s, "http_ms": round(dt * 1000, 1)}
th.join(60); R["rtl"]["hover_after"] = res.get("s"); log("rtl:", R["rtl"])
trace = []
def _rtl_done():
    st = status(); trace.append({"t": round(time.time() - t0, 1), **{k: st.get(k) for k in ("armed", "armed_stale", "connected", "fleet_state", "trajectory_running")}})
    return st.get("armed") == 0 and st.get("fleet_state") in ("DISARMED",)
R["rtl"]["disarmed_after_s"] = wait(_rtl_done, 120, step=3.0)
R["rtl"]["status_trace_every_3s"] = trace[:45]
R["phases"]["rtl"] = {"t0": t0, "t1": time.time()}; time.sleep(3)

# P. cable pull and replug
if MODE == "serial":
    pid = int(open(f"{OUT}/bridge.pid").read()); log("pulling the cable")
    R["pull"] = {"connected_before": status().get("connected")}
    tp = time.time(); os.kill(pid, signal.SIGUSR1)
    R["pull"]["seen_down_after_s"] = wait(lambda: status().get("connected") == 0, 30)
    time.sleep(8); tr = time.time(); os.kill(pid, signal.SIGUSR2); log("cable back in")
    R["pull"]["reconnected_after_replug_s"] = wait(lambda: status().get("connected") == 1 and not status().get("armed_stale"), 120)
    R["pull"]["preflight_green_after_s"] = wait(ready_pre, 120)
    s, b, dt = call("POST", "/api/single-drone/hover-sequence", {"target_altitude_m": 1.0, "hover_duration_s": 3.0}, tok)
    R["pull"]["hover_after"] = {"http": s, "status": (b or {}).get("status"), "detail": None if s == 200 else str(b)[:200]}
    log("pull:", R["pull"])

# analysis of the probe log
rows = [json.loads(l) for l in open(f"{OUT}/cadence.jsonl") if l.strip()]
def rate(name, a, b):
    ts = sorted(r["t"] for r in rows if r["s"] == name and a <= r["t"] <= b)
    if len(ts) < 3: return None
    iv = [y - x for x, y in zip(ts, ts[1:])]
    return {"n": len(ts), "hz": round((len(ts) - 1) / (ts[-1] - ts[0]), 2), "median_interval_s": round(statistics.median(iv), 4), "p99_interval_s": round(sorted(iv)[int(len(iv) * 0.99) - 1], 4), "max_gap_s": round(max(iv), 3)}
R["cadence"] = {ph: {n: rate(n, w["t0"], w["t1"]) for n in ("position_velocity_ned", "landed_state")} for ph, w in R["phases"].items()}
lat = {}
for r in rows:
    if r["s"].startswith("cmd:"): lat.setdefault(r["s"][4:], []).append((r["dt"] * 1000, r["ok"]))
R["command_latency_ms"] = {k: {"n": len(v), "p50": round(statistics.median(x for x, _ in v), 1), "max": round(max(x for x, _ in v), 1), "failed": sum(1 for _, ok in v if not ok)} for k, v in lat.items()}
json.dump(R, open(f"{OUT}/result.json", "w"), indent=1)
print(json.dumps({k: R[k] for k in R if k not in ("phases",)}, indent=1)[:6000])
