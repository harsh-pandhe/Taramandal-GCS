"""The one show representation both halves of the system agree on.

Two dict shapes are in use historically and both must keep working:

  * **Studio shape** — what the choreography engine and the web app exchange::

        {"drones": [{"id": 0, "waypoints": [{"time","x","y","z","yaw",...}]}]}

  * **GCS shape** — what :mod:`tara_gcs.trajectory_parser` produces for the
    flight controller::

        {0: [{"time","x","y","z","yaw"}], 1: [...]}

Rather than pick a winner and break one side, everything in :mod:`tara_core`
operates on :class:`Show` and converts at the boundary.

Coordinates are local NED metres in the shared *fleet frame*: X north, Y east,
Z **down** (so a drone 2 m in the air has ``z == -2.0``). Yaw is degrees.
``r``/``g``/``b`` are optional 0-255 LED channels.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .safety import DEFAULT_LIMITS, SafetyLimits

#: Bumped when the on-disk waypoint schema changes incompatibly.
SHOW_FORMAT_VERSION = 1

_REQUIRED_KEYS = ("time", "x", "y", "z")


@dataclass
class Show:
    """A full multi-drone choreography.

    Attributes:
        drones: Mapping of drone id to its time-ordered waypoint list.
    """

    drones: dict[int, list[dict]] = field(default_factory=dict)

    # ------------------------------------------------------------------ load

    @classmethod
    def from_any(cls, data) -> "Show":
        """Build a :class:`Show` from either supported dict shape.

        Args:
            data: A Studio-shaped dict, a GCS-shaped dict, or an existing
                :class:`Show` (returned unchanged).

        Raises:
            ValueError: If the structure is unrecognised or a waypoint is
                missing a required key.
        """
        if isinstance(data, cls):
            return data
        if not isinstance(data, dict):
            raise ValueError(f"cannot build a Show from {type(data).__name__}")

        if "drones" in data and isinstance(data["drones"], list):
            drones = {}
            for entry in data["drones"]:
                if "id" not in entry:
                    raise ValueError("Studio-shaped drone entry is missing 'id'")
                drones[int(entry["id"])] = list(entry.get("waypoints", []))
        else:
            drones = {int(k): list(v) for k, v in data.items() if not str(k).startswith("_")}

        for drone_id, wps in drones.items():
            for wp in wps:
                missing = [k for k in _REQUIRED_KEYS if k not in wp]
                if missing:
                    raise ValueError(
                        f"drone {drone_id} has a waypoint missing {missing}"
                    )
        return cls(drones=drones)

    # ------------------------------------------------------------------ dump

    def to_studio(self) -> dict:
        """Return the Studio-shaped dict, including the computed duration."""
        return {
            "version": SHOW_FORMAT_VERSION,
            "drones": [
                {"id": drone_id, "waypoints": wps}
                for drone_id, wps in sorted(self.drones.items())
            ],
            "_duration": self.duration,
        }

    def to_gcs(self) -> dict[int, list[dict]]:
        """Return the GCS-shaped ``{drone_id: [waypoints]}`` dict."""
        return dict(self.drones)

    # ------------------------------------------------------------ properties

    @property
    def num_drones(self) -> int:
        return len(self.drones)

    @property
    def duration(self) -> float:
        """Length of the show in seconds (latest waypoint across all drones)."""
        return max(
            (wps[-1]["time"] for wps in self.drones.values() if wps),
            default=0.0,
        )

    @property
    def has_light_data(self) -> bool:
        """True if any waypoint carries LED colour channels."""
        return any(
            "r" in wp for wps in self.drones.values() for wp in wps
        )

    # ------------------------------------------------------------- geofence

    def max_horizontal_radius_m(self) -> float:
        """Max horizontal distance (``sqrt(x**2 + y**2)``) from the origin.

        Scans every waypoint of every drone across the show's full duration —
        entry-blend, the show body and the exit/return — not just the launch
        pad positions at ``t=0``, since a formation can carry a drone further
        from home mid-show than it started.
        """
        return max(
            (math.hypot(wp["x"], wp["y"]) for wps in self.drones.values() for wp in wps),
            default=0.0,
        )

    def geofence_envelope(self, limits: SafetyLimits = DEFAULT_LIMITS) -> dict:
        """Derive this show's two-tier geofence radii from its trajectory envelope.

        Single source of truth for issue #53: computed once here from
        :meth:`max_horizontal_radius_m`, so every consumer (the GCS's runtime
        monitor, the exported show JSON the web UI reads) works from the same
        number instead of each re-deriving or hardcoding it.

        Args:
            limits: Safety limits supplying the soft/hard buffers. Defaults to
                :data:`tara_core.safety.DEFAULT_LIMITS`.

        Returns:
            A dict with ``envelope_radius_m`` (the raw computed envelope),
            ``soft_warning_radius_m`` and ``hard_geofence_radius_m``.
        """
        envelope = self.max_horizontal_radius_m()
        soft, hard = limits.geofence_radii(envelope)
        return {
            "envelope_radius_m": envelope,
            "soft_warning_radius_m": soft,
            "hard_geofence_radius_m": hard,
        }
