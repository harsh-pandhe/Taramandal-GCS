"""Issue #64 over the real API: 401 vs 403, the role on every route, login,
the WebSocket, the command audit trail, and that no secret is ever served.

Marked ``real_auth`` so conftest's logged-in-commander override is off: every
request here goes through the actual bearer-token check.
"""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tara_gcs import main as gcs_main
from tara_gcs.fleet_fsm import FleetStateMachine
from tara_gcs.auth import AuthService, LoginThrottle, OperatorStore, Role, SessionStore

pytestmark = pytest.mark.real_auth

PASSPHRASES = {
    "vera": ("viewer passphrase", Role.VIEWER),
    "chloe": ("choreographer passphrase", Role.CHOREOGRAPHER),
    "fiona": ("commander passphrase", Role.FLIGHT_COMMANDER),
}

#: Routes anyone may call. Everything else must name a role.
PUBLIC = {"/api/status", "/api/auth/login", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = OperatorStore(tmp_path / "operators.json")
    for name, (passphrase, role) in PASSPHRASES.items():
        store.add(name, role, passphrase)
    service = AuthService(store, SessionStore(), LoginThrottle(max_failures=3, window_s=60))
    monkeypatch.setattr(gcs_main, "auth_service", service)
    monkeypatch.setattr(gcs_main.command_audit, "path", str(tmp_path / "command_audit.jsonl"))
    gcs_main.command_audit.recent.clear()
    return store


@pytest.fixture
def client(store):
    # No `with`: the startup hook (vehicle connections) is not needed here.
    return TestClient(gcs_main.app)


def login(client, name):
    res = client.post("/api/auth/login", json={"operator": name, "passphrase": PASSPHRASES[name][0]})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['token']}"}


def required_role(route) -> Role | None:
    """The role a route's dependencies demand, found by walking its dependency tree."""
    stack = [route.dependant]
    while stack:
        dep = stack.pop()
        role = getattr(dep.call, "required_role", None)
        if role is not None:
            return role
        stack.extend(dep.dependencies)
    return None


def http_routes():
    return [
        (method, route.path, required_role(route))
        for route in gcs_main.app.routes
        if isinstance(route, APIRoute) and route.path not in PUBLIC
        for method in sorted(route.methods - {"HEAD", "OPTIONS"})
    ]


def concrete(path):
    return path.replace("{drone_id}", "0")


# --- acceptance criteria ---------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/launch", "/api/rtl"])
def test_unauthenticated_launch_and_rtl_are_401_and_command_nothing(client, monkeypatch, path):
    rtl = AsyncMock()
    monkeypatch.setattr(gcs_main.drone_manager, "trigger_rtl", rtl)
    res = client.post(path)
    assert res.status_code == 401
    assert res.headers["www-authenticate"] == "Bearer"
    bad_token = client.post(path, headers={"Authorization": "Bearer not-a-real-token"})
    assert bad_token.status_code == 401
    rtl.assert_not_called()


@pytest.mark.parametrize("path", [
    "/api/launch", "/api/rtl", "/api/land", "/api/estop", "/api/drone/0/estop",
    "/api/upload-mission", "/api/start-trajectory", "/api/preflight/latch/request",
])
def test_viewer_cannot_arm_launch_or_abort(client, monkeypatch, path):
    launch = AsyncMock()
    monkeypatch.setattr(gcs_main.drone_manager, "start_launch_sequence", launch)
    res = client.post(path, headers=login(client, "vera"), json={})
    assert res.status_code == 403
    assert "vera is a VIEWER" in res.json()["detail"]
    launch.assert_not_called()


def test_choreographer_may_upload_but_not_fly(client):
    headers = login(client, "chloe")
    upload = client.post(
        "/api/upload-trajectory", headers=headers,
        files={"file": ("show.json", b"{}", "application/json")},
    )
    # Past authentication and the role check; what refuses this empty, unsigned
    # file (if anything) is the show validation, not the operator's role.
    assert upload.status_code != 401
    assert "is a CHOREOGRAPHER" not in upload.text
    assert client.post("/api/launch", headers=headers).status_code == 403


def test_every_route_names_a_role(client):
    """No wildcard access: a route added later without a role fails here."""
    unprotected = [(m, p) for m, p, role in http_routes() if role is None]
    assert unprotected == []
    ws = [r for r in gcs_main.app.routes if isinstance(r, APIWebSocketRoute)]
    assert ws and all(any(d.call is gcs_main.ws_operator for d in r.dependant.dependencies) for r in ws)


def test_role_matrix_401_without_a_session_and_403_below_the_role(client):
    sessions = {Role.VIEWER: login(client, "vera"), Role.CHOREOGRAPHER: login(client, "chloe")}
    checked = 0
    for method, path, role in http_routes():
        assert client.request(method, concrete(path)).status_code == 401, (method, path)
        for held, headers in sessions.items():
            if held < role:
                res = client.request(method, concrete(path), headers=headers)
                assert res.status_code == 403, (method, path, held)
                checked += 1
    assert checked >= 20


def test_flight_commands_need_the_commander_role(client):
    roles = {(m, p): r for m, p, r in http_routes()}
    for path in ["/api/launch", "/api/rtl", "/api/land", "/api/estop", "/api/drone/{drone_id}/rtl",
                 "/api/drone/{drone_id}/land", "/api/drone/{drone_id}/estop", "/api/start-trajectory",
                 "/api/stop-trajectory", "/api/upload-mission", "/api/preflight/override/confirm",
                 "/api/sim/launch", "/api/sim/stop"]:
        assert roles[("POST", path)] is Role.FLIGHT_COMMANDER, path
    assert roles[("POST", "/api/upload-trajectory")] is Role.CHOREOGRAPHER
    assert roles[("GET", "/api/fleet-state")] is Role.VIEWER


def test_health_check_stays_public_for_tara_status(client):
    assert client.get("/api/status").status_code == 200


# --- login / session ---------------------------------------------------------------


def test_login_me_logout(client):
    headers = login(client, "fiona")
    assert client.get("/api/auth/me", headers=headers).json() == {"operator": "fiona", "role": "FLIGHT_COMMANDER"}
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_wrong_passphrase_and_unknown_operator_look_the_same(client):
    a = client.post("/api/auth/login", json={"operator": "fiona", "passphrase": "wrong wrong wrong"})
    b = client.post("/api/auth/login", json={"operator": "nobody", "passphrase": "wrong wrong wrong"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_repeated_failures_are_throttled(client):
    for _ in range(3):
        client.post("/api/auth/login", json={"operator": "fiona", "passphrase": "guess guess guess"})
    res = client.post("/api/auth/login", json={"operator": "fiona", "passphrase": PASSPHRASES["fiona"][0]})
    assert res.status_code == 429
    assert int(res.headers["retry-after"]) > 0


def test_removing_an_operator_locks_them_out_immediately(client, store):
    headers = login(client, "fiona")
    store.remove("fiona")
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_demoting_an_operator_applies_to_their_open_session(client, store):
    headers = login(client, "fiona")
    store.add("fiona", Role.VIEWER, PASSPHRASES["fiona"][0], replace=True)
    assert client.post("/api/launch", headers=headers).status_code == 403


# --- identity and audit ------------------------------------------------------------


def test_the_lifecycle_audit_records_the_session_operator_not_a_claimed_one(client, monkeypatch, tmp_path):
    audit_path = tmp_path / "fleet_audit.jsonl"
    monkeypatch.setattr(gcs_main.drone_manager, "fleet_fsm", FleetStateMachine(audit_path=str(audit_path)))
    headers = {**login(client, "fiona"), "X-Operator-ID": "mallory"}
    res = client.post("/api/stop-trajectory", headers=headers)
    assert res.status_code == 409  # nothing is flying; the lifecycle refuses
    rejected = json.loads(audit_path.read_text().splitlines()[-1])
    assert rejected["accepted"] is False
    assert rejected["operator"] == "fiona"


def test_every_command_is_audited_with_operator_role_ip_and_outcome(client, tmp_path):
    client.post("/api/launch")  # anonymous
    client.post("/api/launch", headers=login(client, "vera"))  # wrong role
    commander = login(client, "fiona")
    client.post("/api/stop-trajectory", headers=commander)

    entries = client.get("/api/audit/commands", headers=commander).json()["entries"]
    commands = [e for e in entries if e["event"] == "command"]
    assert [(e["path"], e["status"], e["operator"], e["role"]) for e in commands] == [
        ("/api/launch", 401, None, None),
        ("/api/launch", 403, "vera", "VIEWER"),
        ("/api/stop-trajectory", 409, "fiona", "FLIGHT_COMMANDER"),
    ]
    assert all(e["origin_ip"] and e["ts"] for e in commands)
    logins = [e for e in entries if e["event"] == "login"]
    assert {e["operator"] for e in logins} == {"vera", "fiona"}

    # Written to disk as well, and never with a passphrase in it.
    on_disk = (tmp_path / "command_audit.jsonl").read_text()
    assert '"/api/stop-trajectory"' in on_disk
    for passphrase, _ in PASSPHRASES.values():
        assert passphrase not in on_disk


def test_failed_logins_are_audited_without_the_passphrase(client):
    client.post("/api/auth/login", json={"operator": "fiona", "passphrase": "not the real one!"})
    entries = client.get("/api/audit/commands", headers=login(client, "vera")).json()["entries"]
    failed = [e for e in entries if e["event"] == "login_failed"]
    assert failed and failed[0]["operator"] == "fiona"
    assert "not the real one!" not in json.dumps(entries)


# --- WebSocket -----------------------------------------------------------------------


def test_telemetry_socket_refuses_without_a_token(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/telemetry") as ws:
            ws.receive_bytes()
    assert exc.value.code == 1008


def test_telemetry_socket_accepts_a_viewer_token_offered_as_a_subprotocol(client):
    token = login(client, "vera")["Authorization"].split()[1]
    with client.websocket_connect("/ws/telemetry", subprotocols=[gcs_main.WS_AUTH_PROTOCOL, token]) as ws:
        assert ws.accepted_subprotocol == gcs_main.WS_AUTH_PROTOCOL
        assert isinstance(ws.receive_bytes(), bytes)


# --- secrets -------------------------------------------------------------------------


def test_no_endpoint_or_socket_frame_serves_key_or_credential_material(client, store):
    records = json.loads(store.path.read_text())["operators"]
    secrets_ = [r["hash"] for r in records] + [r["salt"] for r in records]
    secrets_ += [p for p, _ in PASSPHRASES.values()]
    headers = login(client, "fiona")

    bodies = []
    for method, path, _ in http_routes():
        if method == "GET":
            bodies.append(client.get(concrete(path), headers=headers).text)
    bodies.append(client.get("/api/status").text)
    token = headers["Authorization"].split()[1]
    with client.websocket_connect("/ws/telemetry", subprotocols=[gcs_main.WS_AUTH_PROTOCOL, token]) as ws:
        ws.receive_bytes()
        bodies.append(json.dumps(ws.receive_json()))

    served = "\n".join(bodies)
    assert len(bodies) >= 10
    for secret in secrets_:
        assert secret not in served
    assert "PRIVATE KEY" not in served
