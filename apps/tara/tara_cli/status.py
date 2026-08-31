"""`tara status` — one point-in-time answer to "is the stack up and healthy?".

Two independent sources of truth, deliberately kept separate:

* **Processes.** `reset.check_stray_processes` greps the process table for the
  same patterns `tara reset` kills. This answers "did someone start the stack",
  including the case where a process is alive but wedged and answering nothing.
* **The GCS API.** `GET /api/status` on the gcs-api port answers "is it
  actually serving", and is the only place drone/connected/armed counts exist
  at all — the CLI never talks to a vehicle itself.

Splitting them is what makes the exit code meaningful. Processes running but
the API unreachable is the interesting failure (a half-dead stack), and it is
distinguishable from a clean "nothing is running" only because both are
checked. See :func:`collect_status` for the exact rule.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field

from .config import TaraConfig
from .reset import check_stray_processes

#: Seconds to wait on the GCS API before calling it unreachable. Short on
#: purpose: this is a health check meant to be safe in a script loop, so a
#: hung backend must fail fast rather than stall the caller.
PROBE_TIMEOUT_S = 3.0


@dataclass
class FleetStatus:
    """One status snapshot, shaped to be dumped straight to JSON.

    Attributes:
        up: Whether the stack is both running *and* serving — the single
            boolean a script should branch on.
        processes_running: How many known Taramandal processes are alive.
        process_lines: Their ``pgrep -af`` lines, for the human-readable view.
        api_reachable: Whether the GCS API answered the probe.
        api_url: The endpoint that was probed, so a failure is debuggable.
        backend: "px4" or "sim", as reported by the running backend. ``None``
            when unreachable — the config's value would be a guess about a
            process started with different flags.
        num_drones: Fleet size the running backend was started with.
        connected: Drones currently reporting a live link.
        armed: Drones currently armed.
        trajectory_running: Whether a show is playing back right now.
        error: Why the probe failed, when it did.
    """

    up: bool
    processes_running: int
    process_lines: list[str] = field(default_factory=list)
    api_reachable: bool = False
    api_url: str = ""
    backend: str | None = None
    num_drones: int | None = None
    connected: int | None = None
    armed: int | None = None
    trajectory_running: bool | None = None
    error: str | None = None


def probe_api(url: str, *, timeout: float = PROBE_TIMEOUT_S) -> tuple[dict | None, str | None]:
    """GET ``url`` and decode its JSON body.

    Returns:
        ``(payload, None)`` on success, or ``(None, reason)`` on any failure.
        Failures are returned rather than raised because "the API is down" is
        an expected, reportable state here, not an exceptional one.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code} from {url}"
    except (urllib.error.URLError, OSError) as exc:
        return None, f"cannot reach {url}: {exc}"
    except (ValueError, UnicodeDecodeError) as exc:
        return None, f"malformed response from {url}: {exc}"


def collect_status(config: TaraConfig, *, timeout: float = PROBE_TIMEOUT_S) -> FleetStatus:
    """Gather one snapshot of process and fleet health.

    ``up`` is true only when both halves agree: processes are running *and*
    the API answers. A stack with live processes but a dead API is reported
    ``up=False`` with ``processes_running > 0``, which is exactly the
    half-dead case a script needs to catch — the caller can tell it apart from
    an idle machine by that count.
    """
    process_lines = check_stray_processes()
    url = f"http://127.0.0.1:{config.gcs_backend_port}/api/status"
    payload, error = probe_api(url, timeout=timeout)

    status = FleetStatus(
        up=bool(process_lines) and payload is not None,
        processes_running=len(process_lines),
        process_lines=process_lines,
        api_reachable=payload is not None,
        api_url=url,
        error=error,
    )
    if payload is not None:
        status.backend = payload.get("backend")
        status.num_drones = payload.get("num_drones")
        status.connected = payload.get("connected")
        status.armed = payload.get("armed")
        status.trajectory_running = payload.get("trajectory_running")
    return status


def to_json(status: FleetStatus) -> str:
    """Render ``status`` as the machine-readable document `--json` prints."""
    return json.dumps(asdict(status), indent=2)


def to_text(status: FleetStatus) -> str:
    """Render ``status`` as the human-readable report."""
    if not status.processes_running and not status.api_reachable:
        return "[tara] nothing running."

    lines = [f"[tara] {status.processes_running} process(es) running:"]
    lines.extend(f"  {line}" for line in status.process_lines)

    if status.api_reachable:
        lines.append(
            f"[tara] gcs-api OK — backend={status.backend}, drones={status.num_drones}, "
            f"connected={status.connected}, armed={status.armed}"
        )
        if status.trajectory_running:
            lines.append("[tara] a trajectory is currently playing back.")
    else:
        lines.append(f"[tara] gcs-api UNREACHABLE — {status.error}")

    return "\n".join(lines)
