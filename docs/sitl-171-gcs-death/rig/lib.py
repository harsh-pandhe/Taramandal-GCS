"""HTTP helpers for the rig scripts: talk to the GCS on localhost:8090."""
import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8090"
T0 = time.time()


def ts():
    return f"[{time.time() - T0:7.2f}s]"


def req(method, path, body=None, token=None, timeout=120):
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None


def login(operator, passphrase):
    status, body = req("POST", "/api/auth/login", {"operator": operator, "passphrase": passphrase})
    assert status == 200, (status, body)
    return body.get("token") or body.get("access_token")


def status():
    return req("GET", "/api/status")[1]


def wait_ready(token, timeout=150):
    """True once drone 0's pre-arm gates are all green."""
    start = time.time()
    while time.time() - start < timeout:
        code, body = req("GET", "/api/preflight-check", token=token)
        if code == 200:
            drone = body["drones"].get("0")
            if drone and drone["ready"]:
                return True
        time.sleep(2)
    return False
