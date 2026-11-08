# ruff: noqa: E401,E701,E702,F401  (verbatim record of the script that produced the stored results; not reformatted so it stays what ran)
import json, time, threading, urllib.request, urllib.error, uuid, sys
BASE = "http://127.0.0.1:8090"
T0 = time.time()
def ts(): return f"[{time.time()-T0:7.2f}s]"
def req(method, path, body=None, token=None, headers=None, raw=None, timeout=120):
    h = dict(headers or {})
    data = raw
    if body is not None:
        data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
    if token: h["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read())
        except Exception: return e.code, None
def login(op, pw):
    s, b = req("POST", "/api/auth/login", {"operator": op, "passphrase": pw})
    assert s == 200, (s, b)
    return b.get("token") or b.get("access_token")
def status(): return req("GET", "/api/status")[1]
def wait_ready(token, timeout=150):
    t = time.time()
    while time.time() - t < timeout:
        s, b = req("GET", "/api/preflight-check", token=token)
        if s == 200:
            d = b["drones"].get("0")
            if d and d["ready"]: return True
        time.sleep(2)
    return False
def hover_async(token, alt=1.0, hold=5.0):
    out = {}
    def run():
        out["t0"] = time.time()
        out["res"] = req("POST", "/api/single-drone/hover-sequence", {"target_altitude_m": alt, "hover_duration_s": hold}, token=token, timeout=180)
        out["t1"] = time.time()
    th = threading.Thread(target=run); th.start(); return th, out
def tel_poll(stop, samples, period=0.5):
    # /api/status doesn't expose altitude; sample via audit-less route: use ws? keep status only
    while not stop.is_set():
        samples.append((time.time(), status())); time.sleep(period)
def wait_alt(min_alt, timeout=60):
    """Poll PX4-side altitude via the GCS's telemetry log line? Use /api/export-telemetry."""
    pass
