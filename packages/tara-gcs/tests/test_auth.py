"""Issue #64: operator accounts, sessions and login throttling (no HTTP here;
the API-level 401/403 behaviour is in test_auth_api.py)."""

import json
import os
import stat
from pathlib import Path

import pytest

from tara_gcs.auth import (
    AuthError,
    AuthService,
    InsecureFileError,
    LoginThrottle,
    Operator,
    OperatorStore,
    Role,
    SessionStore,
    load_auth_config,
    main,
    parse_role,
)

PASS = "correct horse battery"


@pytest.fixture
def store(tmp_path):
    return OperatorStore(tmp_path / "ops" / "operators.json")


class TestRoles:
    def test_roles_are_ordered(self):
        assert Role.VIEWER < Role.CHOREOGRAPHER < Role.FLIGHT_COMMANDER

    @pytest.mark.parametrize("text,role", [
        ("viewer", Role.VIEWER), ("CHOREOGRAPHER", Role.CHOREOGRAPHER),
        ("flight_commander", Role.FLIGHT_COMMANDER), ("flight-commander", Role.FLIGHT_COMMANDER),
    ])
    def test_parse_role(self, text, role):
        assert parse_role(text) is role

    def test_unknown_role_is_rejected(self):
        with pytest.raises(ValueError, match="unknown role"):
            parse_role("admin")


class TestOperatorStore:
    def test_add_then_verify(self, store):
        store.add("alice", Role.FLIGHT_COMMANDER, PASS)
        assert store.verify("alice", PASS) == Operator("alice", Role.FLIGHT_COMMANDER)
        assert store.verify("alice", PASS + "x") is None
        assert store.verify("mallory", PASS) is None

    def test_file_is_created_owner_only_and_holds_no_passphrase(self, store):
        store.add("alice", Role.VIEWER, PASS)
        if os.name == "posix":
            assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        text = store.path.read_text()
        assert PASS not in text
        rec = json.loads(text)["operators"][0]
        assert set(rec) == {"id", "role", "salt", "hash", "scrypt"}

    def test_same_passphrase_gets_different_hashes(self, store):
        store.add("alice", Role.VIEWER, PASS)
        store.add("bob", Role.VIEWER, PASS)
        recs = json.loads(store.path.read_text())["operators"]
        assert recs[0]["hash"] != recs[1]["hash"]

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
    @pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o644])
    def test_refuses_a_file_others_can_read(self, store, mode):
        store.add("alice", Role.VIEWER, PASS)
        os.chmod(store.path, mode)
        with pytest.raises(InsecureFileError, match="chmod 600"):
            OperatorStore(store.path).get("alice")

    def test_rejects_short_passphrases_bad_ids_and_duplicates(self, store):
        with pytest.raises(ValueError, match="at least"):
            store.add("alice", Role.VIEWER, "short")
        with pytest.raises(ValueError, match="operator id"):
            store.add("../root", Role.VIEWER, PASS)
        store.add("alice", Role.VIEWER, PASS)
        with pytest.raises(ValueError, match="already exists"):
            store.add("alice", Role.FLIGHT_COMMANDER, PASS)

    def test_replace_changes_role_and_passphrase(self, store):
        store.add("alice", Role.VIEWER, PASS)
        store.add("alice", Role.FLIGHT_COMMANDER, "a brand new passphrase", replace=True)
        assert store.verify("alice", PASS) is None
        assert store.get("alice").role is Role.FLIGHT_COMMANDER

    def test_picks_up_changes_made_by_another_process(self, store):
        reader = OperatorStore(store.path)
        assert reader.get("alice") is None
        store.add("alice", Role.VIEWER, PASS)
        assert reader.get("alice") == Operator("alice", Role.VIEWER)
        store.remove("alice")
        assert reader.get("alice") is None

    def test_missing_file_means_no_operators(self, store):
        assert store.all() == []
        assert store.verify("alice", PASS) is None


class TestSessions:
    def test_issue_resolve_and_expire(self):
        sessions = SessionStore(ttl_s=60)
        token, _ = sessions.issue(Operator("alice", Role.VIEWER), now=1000)
        assert sessions.resolve(token, now=1059).operator_id == "alice"
        assert sessions.resolve(token, now=1060) is None
        assert sessions.resolve(token, now=1000) is None  # expired ones are dropped

    def test_tokens_are_long_random_and_stored_hashed(self):
        sessions = SessionStore()
        a, _ = sessions.issue(Operator("alice", Role.VIEWER))
        b, _ = sessions.issue(Operator("alice", Role.VIEWER))
        assert a != b and len(a) >= 40
        assert a not in sessions._sessions and b not in sessions._sessions

    def test_revoke(self):
        sessions = SessionStore()
        token, _ = sessions.issue(Operator("alice", Role.VIEWER))
        other, _ = sessions.issue(Operator("bob", Role.VIEWER))
        assert sessions.revoke(token)
        assert sessions.resolve(token) is None
        assert sessions.revoke_operator("bob") == 1
        assert sessions.resolve(other) is None


class TestThrottle:
    def test_locks_out_after_repeated_failures_then_recovers(self):
        t = LoginThrottle(max_failures=3, window_s=60)
        for i in range(3):
            assert t.retry_after("1.2.3.4", now=100 + i) == 0
            t.failed("1.2.3.4", now=100 + i)
        assert t.retry_after("1.2.3.4", now=103) > 0
        assert t.retry_after("5.6.7.8", now=103) == 0  # per origin
        assert t.retry_after("1.2.3.4", now=161) == 0

    def test_success_clears_the_count(self):
        t = LoginThrottle(max_failures=2, window_s=60)
        t.failed("ip", now=1)
        t.succeeded("ip")
        t.failed("ip", now=2)
        assert t.retry_after("ip", now=3) == 0


class TestAuthService:
    def test_login_and_authenticate(self, store):
        store.add("alice", Role.CHOREOGRAPHER, PASS)
        auth = AuthService(store)
        token, session, op = auth.login("alice", PASS, origin="127.0.0.1")
        assert op == Operator("alice", Role.CHOREOGRAPHER)
        assert auth.authenticate(token) == op

    def test_bad_login_and_lockout(self, store):
        store.add("alice", Role.VIEWER, PASS)
        auth = AuthService(store, throttle=LoginThrottle(max_failures=2, window_s=60))
        for _ in range(2):
            with pytest.raises(AuthError, match="wrong passphrase"):
                auth.login("alice", "nope nope nope", origin="ip")
        with pytest.raises(AuthError, match="Too many failed logins"):
            auth.login("alice", PASS, origin="ip")

    @pytest.mark.parametrize("token", [None, "", "not-a-token"])
    def test_missing_or_unknown_token(self, store, token):
        with pytest.raises(AuthError):
            AuthService(store).authenticate(token)

    def test_removing_an_operator_ends_their_session(self, store):
        store.add("alice", Role.FLIGHT_COMMANDER, PASS)
        auth = AuthService(store)
        token, _, _ = auth.login("alice", PASS, origin="ip")
        store.remove("alice")
        with pytest.raises(AuthError, match="no longer exists"):
            auth.authenticate(token)

    def test_a_role_change_applies_to_the_open_session(self, store):
        store.add("alice", Role.FLIGHT_COMMANDER, PASS)
        auth = AuthService(store)
        token, _, _ = auth.login("alice", PASS, origin="ip")
        store.add("alice", Role.VIEWER, PASS, replace=True)
        assert auth.authenticate(token).role is Role.VIEWER


class TestConfig:
    def test_defaults(self, tmp_path):
        cfg = load_auth_config(tmp_path / "missing.toml", environ={})
        assert cfg.operators_path.name == "operators.json"
        assert cfg.session_ttl_s == 12 * 3600

    def test_toml_and_env(self, tmp_path):
        toml = tmp_path / "tara.toml"
        toml.write_text('[auth]\noperators_file = "/x/ops.json"\nsession_ttl_hours = 2\n')
        assert load_auth_config(toml, environ={}).operators_path == Path("/x/ops.json")
        assert load_auth_config(toml, environ={}).session_ttl_s == 7200
        env = {"GCS_OPERATORS_PATH": str(tmp_path / "env.json")}
        assert load_auth_config(toml, environ=env).operators_path == tmp_path / "env.json"

    def test_bad_ttl(self, tmp_path):
        toml = tmp_path / "tara.toml"
        toml.write_text("[auth]\nsession_ttl_hours = 0\n")
        with pytest.raises(ValueError):
            load_auth_config(toml, environ={})


class TestCli:
    def test_add_list_remove(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "operators.json"
        monkeypatch.setattr("getpass.getpass", lambda prompt="": PASS)
        assert main(["--file", str(path), "add", "alice", "--role", "flight_commander"]) == 0
        assert main(["--file", str(path), "list"]) == 0
        out = capsys.readouterr().out
        assert "alice\tFLIGHT_COMMANDER" in out and PASS not in out
        assert main(["--file", str(path), "remove", "alice"]) == 0
        assert main(["--file", str(path), "remove", "alice"]) == 1

    def test_mismatched_passphrases_are_refused(self, tmp_path, monkeypatch):
        answers = iter([PASS, PASS + "!"])
        monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
        assert main(["--file", str(tmp_path / "o.json"), "add", "alice", "--role", "viewer"]) == 1
        assert not (tmp_path / "o.json").exists()
