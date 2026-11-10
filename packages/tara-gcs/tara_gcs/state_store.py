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


# The encoded trajectory of the show that is playing, with what it was encoded from.
_trajectory_cache: dict = {"ref": None, "fingerprint": None, "json": None}


def _trajectory_json(trajectory_data: dict) -> str:
    """JSON object of the trajectory with string keys, encoded once per loaded show.

    Reused while the same trajectory object is presented with the same slots and
    waypoint-list lengths. A trajectory is replaced as a whole when a new show is
    loaded, never edited in place, so a different show always misses the cache.
    """
    fingerprint = tuple((k, id(v), len(v)) for k, v in trajectory_data.items())
    cache = _trajectory_cache
    if cache["ref"] is trajectory_data and cache["fingerprint"] == fingerprint:
        return cache["json"]
    text = json.dumps({str(drone_id): wps for drone_id, wps in trajectory_data.items()})
    cache.update(ref=trajectory_data, fingerprint=fingerprint, json=text)
    return text


def prime_trajectory_cache(trajectory_data: dict) -> None:
    """Encodes the trajectory now, so the first snapshot of a show is not the one that pays for it.

    Called when the show's missions are uploaded: nothing is flying yet, whereas the first snapshot
    happens a few seconds into the show.
    """
    _trajectory_json(trajectory_data)


def release_trajectory_cache() -> None:
    """Drops the cached encoding (a show that is no longer playing needs none)."""
    _trajectory_cache.update(ref=None, fingerprint=None, json=None)


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
    # The trajectory is by far the largest part (200 MB at 269 drones x 9,722
    # waypoints) and does not change while a show plays, so it is encoded once and
    # reused by every later snapshot (see _trajectory_json). Re-encoding it every
    # STATE_SNAPSHOT_INTERVAL_S froze the event loop for 2.6-3.5 s each time, which
    # stalled the simulated fleet behind its plan. The result is the same JSON
    # document the dict payload used to produce.
    head = (
        '{"version": 1, "saved_at": ' + json.dumps(time.time())
        + ', "mode": ' + json.dumps(mode)
        + ', "show_started_at": ' + json.dumps(show_started_at)
        + ', "trajectory": '
    )
    tail = ', "telemetry": ' + json.dumps(telemetry) + "}"
    trajectory_text = _trajectory_json(trajectory_data)

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".show_state_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            # dumps, not dump: dump streams through json's pure-Python encoder,
            # dumps uses the C one. Same output, but at 1,000 drones it is
            # ~5ms instead of ~23ms of event-loop time every snapshot, which
            # was enough on its own to make physics ticks late (issue #51).
            # Three writes rather than one concatenation: joining would copy the
            # (hundreds of MB) trajectory text again on every snapshot.
            f.write(head)
            f.write(trajectory_text)
            f.write(tail)
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
    release_trajectory_cache()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
