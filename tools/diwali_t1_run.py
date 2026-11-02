#!/usr/bin/env python3
"""Flies a signed Diwali show through a running GCS, the way the demo does (T1, `tara_sim`).

Drives the same GCS API the dashboard buttons call: login, signed upload, mission upload,
the two-step pre-arm latch, launch, onboard start, then waits for SHOW_COMPLETE and lands.
It exists so the T1 demo can be rehearsed and timed from a terminal, and so a stalled or
aborted run is reported rather than eyeballed. It never changes a safety setting.

Needs a stack already up (`./tara up --backend sim -n N`) and the show signed with
`./tara sign`. See docs/diwali-runbook.md.

    python tools/diwali_t1_run.py dist/diwali/diwali_35.json --operator runbook
    TARA_PASSPHRASE=... python tools/diwali_t1_run.py dist/diwali/diwali_100.json

Exit code 0 only if the fleet reached SHOW_COMPLETE with no aborted drone and no emergency
transition, and landed. Anything else prints why and exits 1; do not count that run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sitl_planned_vs_actual import Api, RunError, multipart  # noqa: E402

EMERGENCY = {"RTL", "LANDING", "ESTOP"}
DONE = {"SHOW_COMPLETE", "DISARMED"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_for(gcs: Api, targets: set[str], timeout: float, stop_on: set[str]) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        status, st = gcs.call("GET", "/api/status", timeout=15)
        if status == 200:
            if st["fleet_state"] != last:
                last = st["fleet_state"]
                log(f"fleet_state={last} connected={st['connected']} armed={st['armed']}")
            if st["fleet_state"] in targets | stop_on:
                return st
        time.sleep(0.5)
    raise RunError(f"timeout after {timeout:.0f}s waiting for {sorted(targets)} (last state: {last})")


def run(args: argparse.Namespace) -> int:
    show = Path(args.show)
    sig = Path(args.sig or f"{args.show}.sig.json")
    if not sig.exists():
        raise RunError(f"{sig} not found: sign the show first with `./tara sign {show} --key <key>`")

    log(f"host load average (1/5/15 min): {os.getloadavg()[0]:.1f} / {os.getloadavg()[1]:.1f} / {os.getloadavg()[2]:.1f} on {os.cpu_count()} cores")
    gcs = Api(args.gcs)
    token = gcs.must("POST", "/api/auth/login", {"operator": args.operator, "passphrase": args.passphrase})["token"]
    gcs.token = token
    st = gcs.must("GET", "/api/status")
    log(f"GCS: backend={st['backend']} fidelity={st['fidelity']} connected={st['connected']}/{st['num_drones']} "
        f"fleet_state={st['fleet_state']}")
    if st["fleet_state"] != "STAGED":
        raise RunError(f"fleet is {st['fleet_state']}, not STAGED: restart the stack between runs")

    body, ctype = multipart({
        "file": (show.name, show.read_bytes(), "application/json"),
        "signature": (sig.name, sig.read_bytes(), "application/json"),
    })
    up = gcs.must("POST", "/api/upload-trajectory", raw=body, content_type=ctype, timeout=300)
    if not up.get("signature", {}).get("verified"):
        raise RunError(f"signature not verified: {up.get('signature')}")
    log(f"trajectory uploaded, signature verified (key {up['signature'].get('key_id') or up['signature']})")
    planned = max(d["duration_seconds"] for d in up["drones_summary"].values())

    gcs.must("POST", "/api/upload-mission", timeout=600)
    ms = gcs.must("GET", "/api/mission-status")
    if not ms.get("launch_gate", {}).get("ok"):
        raise RunError(f"launch gate closed after mission upload: {ms.get('launch_gate')}")
    log(f"missions verified on {ms.get('verified_count')} drones, launch gate open")

    pre = gcs.must("GET", "/api/preflight-check")
    if not pre.get("launch_permitted"):
        raise RunError(f"pre-arm checklist blocks launch: {pre.get('blocking_reasons')}")
    latch = gcs.must("POST", "/api/preflight/latch/request", {})
    gcs.must("POST", "/api/preflight/latch/confirm", {"token": latch["token"]})
    t_launch = time.time()
    gcs.must("POST", "/api/launch")
    log("launch accepted")
    st = wait_for(gcs, {"FORMATION_HOLD"}, args.launch_timeout, EMERGENCY | {"STAGED", "DISARMED"})
    if st["fleet_state"] != "FORMATION_HOLD":
        raise RunError(f"launch ended in {st['fleet_state']}, not FORMATION_HOLD")
    log(f"FORMATION_HOLD {time.time() - t_launch:.0f}s after launch")
    time.sleep(args.hold_s)

    t0 = time.time()
    gcs.must("POST", "/api/start-trajectory", {"mode": "LOCAL", "execution": args.execution})
    log(f"show started ({args.execution}), planned {planned:.1f}s")
    st = wait_for(gcs, DONE, planned + args.slack_s, EMERGENCY)
    show_wall = time.time() - t0
    fleet = gcs.must("GET", "/api/fleet-state")
    bad = [h for h in fleet.get("history", []) if h.get("to") in EMERGENCY and h.get("ts", 0) >= t0]
    aborted = st.get("aborted") or []
    log(f"end state {st['fleet_state']} after {show_wall:.1f}s wall (planned {planned:.1f}s); "
        f"aborted={aborted}; emergency transitions={[h['to'] for h in bad]}")

    landed = None
    if st["fleet_state"] == "SHOW_COMPLETE" and not bad and not aborted:
        gcs.call("POST", "/api/land", timeout=60)
        st = wait_for(gcs, {"DISARMED"}, args.land_timeout, set())
        landed = st["fleet_state"] == "DISARMED"
        log(f"landed and disarmed: {landed}")
    ok = bool(landed) and not bad and not aborted
    log(f"host load average at end: {os.getloadavg()[0]:.1f}")
    print(json.dumps({"show": show.name, "ok": ok, "planned_s": planned, "show_wall_s": round(show_wall, 1),
                      "end_state": st["fleet_state"], "aborted": aborted,
                      "emergency": [h["to"] for h in bad], "load_at_end": round(os.getloadavg()[0], 1)}, indent=2))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("show", help="exported show (.json or .csv, under the GCS 5 MB / 100,000-row upload limits), signed")
    ap.add_argument("--sig", help="signature manifest (default: SHOW.sig.json)")
    ap.add_argument("--gcs", default="http://127.0.0.1:8090")
    ap.add_argument("--operator", default=os.environ.get("TARA_OPERATOR", ""))
    ap.add_argument("--passphrase", default=os.environ.get("TARA_PASSPHRASE", ""))
    ap.add_argument("--execution", choices=("onboard", "streamed"), default="onboard",
                    help="onboard for backend=sim; streamed for PX4 SITL (no onboard executor there)")
    ap.add_argument("--launch-timeout", type=float, default=600)
    ap.add_argument("--hold-s", type=float, default=3.0)
    ap.add_argument("--slack-s", type=float, default=90)
    ap.add_argument("--land-timeout", type=float, default=180)
    args = ap.parse_args()
    if not args.operator:
        ap.error("--operator (or TARA_OPERATOR) is required")
    try:
        return run(args)
    except RunError as exc:
        log(f"RUN FAILED, do not count it: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
