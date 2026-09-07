"""Local-file persistence for the active show, so a GCS restart mid-flight
doesn't lose track of where the fleet was (issue #4).

Deliberately not a database: one JSON file, written atomically, holding just
enough to reload the active trajectory and resume playback from the same
point in the timeline. Fleet telemetry is included as a last-known-position
snapshot for the operator's benefit, not as something GCS acts on — arming or
launching a drone always requires a fresh explicit operator action.
"""
import json
import logging
import os
import tempfile
import time

logger = logging.getLogger("TaramandalGCS")

DEFAULT_STATE_PATH = os.path.join(os.path.dirname(__file__), "show_state.json")


def save_show_state(
    path: str,
    trajectory_data: dict,
    mode: str,
    show_started_at: float,
    telemetry: dict,
) -> None:
    """Writes the active trajectory + playback position to `path`.

    `show_started_at` is a `time.time()`-based (wall-clock) timestamp of when
    the current trajectory began playing, so `elapsed = time.time() -
    show_started_at` reconstructs playback position after a restart even
    though the original run's `asyncio` monotonic clock is gone.

    Written via write-to-temp-then-replace so a crash mid-write can't leave a
    half-written file that fails to load on the next startup.
    """
    payload = {
        "version": 1,
        "saved_at": time.time(),
        "mode": mode,
        "show_started_at": show_started_at,
        # JSON object keys must be strings; drone ids are restored to int on load.
        "trajectory": {str(drone_id): wps for drone_id, wps in trajectory_data.items()},
        "telemetry": telemetry,
    }

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".show_state_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def load_show_state(path: str) -> dict | None:
    """Returns the saved show state dict, or None if no valid saved state exists.

    Never raises on a missing or corrupt file — a startup-time state check
    failing should fall back to "no saved state" rather than blocking GCS
    from starting.
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            payload = json.load(f)
        payload["trajectory"] = {int(k): v for k, v in payload["trajectory"].items()}
        payload["telemetry"] = {int(k): v for k, v in payload["telemetry"].items()}
        return payload
    except Exception as e:
        logger.error(f"Failed to load saved show state from {path}: {e}")
        return None


def discard_show_state(path: str) -> None:
    """Deletes the saved state file, if present. Safe to call when none exists."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
