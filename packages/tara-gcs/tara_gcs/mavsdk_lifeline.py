"""A mavsdk_server that cannot outlive the GCS (issue #171).

MAVSDK's offboard plugin repeats the last setpoint from inside mavsdk_server,
not from Python. So when the GCS dies and its server does not, PX4 keeps
receiving setpoints from nobody and its offboard-loss failsafe never fires:
seen on real PX4 SITL, a SIGKILLed GCS left the vehicle hovering armed in
OFFBOARD for as long as anyone watched. MAVSDK's own cleanup is an atexit hook,
which neither SIGKILL, an unhandled SIGTERM nor a crash ever runs.

Linux's parent-death signal is the one mechanism that covers all of those,
because the kernel delivers it rather than anything in the dying process. It has
to be set by the child itself, and setting it from ``preexec_fn`` is not safe in
a process that has gRPC's threads running (fork can inherit a held lock), so the
server starts through a throwaway interpreter that sets it and then execs the
real binary in place: same pid, and the signal setting survives the exec.

The signal follows the *thread* that spawned the child, not just the process.
DroneManager connects from the event loop thread, which lives as long as the
GCS. Never connect a GcsBoundSystem from a short-lived worker thread: the server
would die with that thread.

Other platforms have no such signal: the server starts the stock way, with a
warning, and a dead GCS there still leaves the vehicle hovering.
"""

import atexit
import logging
import os
import subprocess
import sys
from importlib.resources import as_file, files

from mavsdk import System
from mavsdk import bin as mavsdk_bin
from mavsdk.system import _LoggingThread

#: Run as ``python -I -S -c LAUNCHER <gcs pid> <server binary> <server args...>``.
#: The getppid check closes the race where the GCS dies before prctl runs, which
#: prctl alone would never notice. Any failure ends here, with no server and the
#: reason in the mavsdk_server log; connect() then waits the way it does for a
#: vehicle that never answers. An unbound server is never started instead.
LAUNCHER = """
import ctypes, os, signal, sys
PR_SET_PDEATHSIG = 1
if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0) != 0:
    sys.exit("mavsdk_lifeline: prctl(PR_SET_PDEATHSIG) failed, errno %d" % ctypes.get_errno())
if os.getppid() != int(sys.argv[1]):
    sys.exit("mavsdk_lifeline: the GCS died before mavsdk_server started")
os.execv(sys.argv[2], sys.argv[2:])
"""


class GcsBoundSystem(System):
    """A MAVSDK System whose embedded mavsdk_server dies with this process.

    Overrides the one step of ``System.connect`` that spawns the server, so
    reconnects, ``_stop_mavsdk_server`` and everything else stay MAVSDK's own.
    """

    @staticmethod
    def _start_mavsdk_server(system_address, port, sysid, compid):
        if not sys.platform.startswith("linux"):
            logging.warning(
                "mavsdk_server is not bound to the GCS on %s: if the GCS dies mid-flight the server "
                "keeps streaming its last setpoint and PX4's offboard failsafe will not fire.",
                sys.platform,
            )
            return System._start_mavsdk_server(system_address, port, sysid, compid)

        with as_file(files(mavsdk_bin) / "mavsdk_server") as binary:
            command = [
                sys.executable, "-I", "-S", "-c", LAUNCHER, str(os.getpid()),
                os.fspath(binary), "-p", str(port), "--sysid", str(sysid), "--compid", str(compid),
            ]
            if system_address:
                command.append(system_address)
            process = subprocess.Popen(command, shell=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        _LoggingThread(process.stdout, logging.getLogger("mavsdk_server")).start()
        atexit.register(process.kill)
        return process
