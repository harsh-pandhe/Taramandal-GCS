"""A dead GCS must not leave mavsdk_server feeding PX4 setpoints (issue #171).

Seen on real PX4 v1.16.2 SITL: SIGKILL of the GCS mid-hover left the vehicle
hovering armed in OFFBOARD for the full 30 s watched. MAVSDK's offboard plugin
repeats the last setpoint from inside mavsdk_server, the server outlived the
GCS, and so PX4's offboard-loss failsafe (COM_OF_LOSS_T) never fired.

These run the real binary under a real DroneManager in a child process and kill
that process, because the property under test belongs to the operating system:
no in-process handler runs on SIGKILL. A crash (SIGSEGV) is left out only
because it writes a core dump. No PX4 is needed, the server only has to be up.
That PX4 then fails safe is shown on SITL; see the PR.
"""

import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from mavlink_heartbeat import HeartbeatSource  # noqa: E402

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="relies on Linux parent-death signals")

UDP_PORT = 14977
GRPC_PORT = 50051  # DroneManager uses 50051 + drone_id


GCS_CHILD = textwrap.dedent(
    f"""
    import asyncio
    from tara_gcs.drone_manager import DroneManager

    async def main():
        manager = DroneManager(ports=[{UDP_PORT}], backend="px4")
        await manager.connect_all()
        print("READY", flush=True)
        await asyncio.sleep(120)

    asyncio.run(main())
    """
)


def _mavsdk_servers_of(parent_pid: int) -> list[int]:
    """Direct children of parent_pid that are running the mavsdk_server binary."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
            # The field after the parenthesised comm: state, then ppid.
            fields = (entry / "stat").read_text().rpartition(")")[2].split()
        except OSError:
            continue
        if comm == "mavsdk_server" and int(fields[1]) == parent_pid:
            found.append(int(entry.name))
    return found


def _running(pid: int) -> bool:
    """True if pid exists and is not a zombie waiting to be reaped."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0] != "Z"
    except OSError:
        return False


def _wait_until(predicate, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@pytest.fixture
def gcs_with_server():
    """A real DroneManager in a child process, with its real mavsdk_server up."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if probe.connect_ex(("127.0.0.1", GRPC_PORT)) == 0:
            pytest.skip(f"gRPC port {GRPC_PORT} is taken, probably by another GCS on this machine")

    servers: list[int] = []
    with HeartbeatSource(UDP_PORT):
        child = subprocess.Popen(
            [sys.executable, "-W", "ignore", "-c", GCS_CHILD],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
        )
        try:
            assert child.stdout.readline().strip() == "READY"
            servers = _mavsdk_servers_of(child.pid)
            assert len(servers) == 1
            yield child, servers[0]
        finally:
            child.kill()
            child.wait()
            for pid in servers:
                if _running(pid):
                    os.kill(pid, signal.SIGKILL)


def test_server_stays_up_while_the_gcs_lives(gcs_with_server):
    """The other half of the property: the fix must not kill a healthy link."""
    child, server = gcs_with_server
    time.sleep(2.0)
    assert child.poll() is None
    assert _running(server)


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGTERM, signal.SIGHUP], ids=lambda s: s.name)
def test_server_dies_with_the_gcs(gcs_with_server, sig):
    """SIGKILL is the #171 case. An unhandled SIGTERM or SIGHUP (a dropped terminal) skips atexit just the same."""
    child, server = gcs_with_server
    assert _running(server)

    child.send_signal(sig)
    child.wait(timeout=5.0)

    # Well inside PX4's default 1 s COM_OF_LOSS_T plus the margin a watchdog
    # would need, so the setpoint stream stops about when the GCS does.
    assert _wait_until(lambda: not _running(server), timeout_s=2.0), (
        f"mavsdk_server {server} outlived the GCS and is still feeding setpoints"
    )


def test_launcher_refuses_when_the_gcs_is_already_gone():
    """The race prctl can't see: the GCS died between fork and prctl. No server may start."""
    from tara_gcs.mavsdk_lifeline import LAUNCHER

    not_our_parent = str(os.getpid() + 1_000_000)
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", LAUNCHER, not_our_parent, "/bin/echo", "SERVER STARTED"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "SERVER STARTED" not in result.stdout
    assert "died before mavsdk_server started" in result.stderr


def test_launcher_execs_in_place_with_the_death_signal_set():
    """Same pid after exec, and PR_SET_PDEATHSIG survives it (read back from the kernel)."""
    from tara_gcs.mavsdk_lifeline import LAUNCHER

    probe = (
        "import ctypes, os, signal; s = ctypes.c_int(); "
        "ctypes.CDLL(None).prctl(2, ctypes.byref(s), 0, 0, 0); "
        "print(os.getpid(), s.value)"
    )
    child = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", LAUNCHER, str(os.getpid()), sys.executable, "-I", "-S", "-c", probe],
        stdout=subprocess.PIPE,
        text=True,
    )
    out, _ = child.communicate(timeout=10)
    pid, deathsig = out.split()
    assert int(pid) == child.pid
    assert int(deathsig) == signal.SIGKILL


def test_other_platforms_fall_back_to_the_stock_server_with_a_warning(monkeypatch, caplog):
    from tara_gcs import mavsdk_lifeline

    started = []
    monkeypatch.setattr(mavsdk_lifeline.sys, "platform", "darwin")
    monkeypatch.setattr(
        mavsdk_lifeline.System, "_start_mavsdk_server", staticmethod(lambda *a: started.append(a) or "stock")
    )
    assert mavsdk_lifeline.GcsBoundSystem._start_mavsdk_server("udpin://x", 50051, 245, 190) == "stock"
    assert started == [("udpin://x", 50051, 245, 190)]
    assert "not bound to the GCS" in caplog.text
