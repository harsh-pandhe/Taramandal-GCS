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

Two ways in, deliberately:

  * :meth:`Show.from_any` is lenient — it only checks the four coordinate
    keys exist — and is what every internal caller uses.
  * :meth:`Show.validate_and_load` is the strict gate for show *files*: it
    checks a Studio-shaped dict against :class:`ShowContainer`, the Pydantic
    mirror of ``schemas/show_v1.json`` (issue #55), and raises a
    :class:`pydantic.ValidationError` whose ``.errors()`` name the exact
    field, e.g. ``('drones', 0, 'waypoints', 12, 'yaw')``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from .layout import MAX_FLEET_SIZE

from .safety import DEFAULT_LIMITS, SafetyLimits

#: Bumped when the on-disk waypoint schema changes incompatibly.
SHOW_FORMAT_VERSION = 1

#: The JSON Schema the models below mirror. Path only; nothing here reads it.
SHOW_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "show_v1.json"

_REQUIRED_KEYS = ("time", "x", "y", "z")


# ------------------------------------------------------------------ schema


def _not_null(value: Any) -> Any:
    """Optional fields may be left out, but an explicit null is an error.

    A ``null`` LED channel would otherwise sail through here and crash the
    .tara compiler at ``int(None)``; the JSON Schema has the same rule.
    """
    if value is None:
        raise ValueError("may be omitted, but not null")
    return value


# strict: no "1.5" -> 1.5 or True -> 1 coercion, so the model agrees with the
# JSON Schema's types. allow_inf_nan=False: NaN/inf aren't valid JSON, but a
# Python dict can still carry them. extra="allow": unknown keys pass through
# untouched at every level, as in the schema.
_SHOW_MODEL_CONFIG = ConfigDict(strict=True, allow_inf_nan=False, extra="allow")

_LedChannel = Annotated[int | None, BeforeValidator(_not_null)]
_OptionalFloat = Annotated[float | None, BeforeValidator(_not_null)]


class Waypoint(BaseModel):
    """One timed position (and optional light state) of a drone."""

    model_config = _SHOW_MODEL_CONFIG

    time: float = Field(..., ge=0.0, description="Seconds since show start")
    x: float = Field(..., description="North, metres")
    y: float = Field(..., description="East, metres")
    z: float = Field(..., description="Down, metres (negative is above the ground)")
    yaw: float = Field(..., ge=0.0, lt=360.0, description="Heading, degrees, wrapped into [0, 360)")
    r: _LedChannel = Field(None, ge=0, le=255, description="Red LED channel, 0-255")
    g: _LedChannel = Field(None, ge=0, le=255, description="Green LED channel, 0-255")
    b: _LedChannel = Field(None, ge=0, le=255, description="Blue LED channel, 0-255")
    w: _LedChannel = Field(None, ge=0, le=255, description="White LED channel, 0-255")
    vx: _OptionalFloat = Field(None, description="North velocity, m/s")
    vy: _OptionalFloat = Field(None, description="East velocity, m/s")
    vz: _OptionalFloat = Field(None, description="Down velocity, m/s")


class DroneTrack(BaseModel):
    """A single drone's id and waypoint list."""

    model_config = _SHOW_MODEL_CONFIG

    id: int = Field(..., ge=0, lt=MAX_FLEET_SIZE, description="Drone id, 0 to MAX_FLEET_SIZE - 1")
    waypoints: list[Waypoint] = Field(..., description="The drone's waypoints")


class GeofenceEnvelope(BaseModel):
    """Two-tier geofence radii computed from the show's envelope (issue #53)."""

    model_config = _SHOW_MODEL_CONFIG

    envelope_radius_m: float = Field(..., ge=0.0, description="Max horizontal reach of any waypoint")
    soft_warning_radius_m: float = Field(..., ge=0.0, description="Radius that raises a warning")
    hard_geofence_radius_m: float = Field(..., ge=0.0, description="Radius that triggers RTL")


class ShowContainer(BaseModel):
    """A whole show file: the Pydantic mirror of ``schemas/show_v1.json``."""

    model_config = _SHOW_MODEL_CONFIG

    # Any version >= 1 is accepted, including ones newer than this code, so a
    # file written by a newer Studio still loads. Deciding what the GCS does
    # with a version it doesn't understand (refuse, warn, degrade) is future
    # work on the GCS read side, tracked as a follow-up to issue #55.
    version: int = Field(..., ge=1, description="Show format version; writers stamp SHOW_FORMAT_VERSION")
    drones: list[DroneTrack] = Field(..., description="One entry per drone")
    geofence: Annotated[GeofenceEnvelope | None, BeforeValidator(_not_null)] = Field(
        None, alias="_geofence", description="Geofence radii, when the exporter computed them"
    )
    signature_ref: str | None = Field(
        None,
        min_length=1,
        description="Filename of the detached signature manifest (issue #49); never the signature itself",
    )

    @field_validator("drones")
    @classmethod
    def _unique_ids(cls, drones: list[DroneTrack]) -> list[DroneTrack]:
        # Not expressible in the JSON Schema. Show keys drones by id, so a
        # duplicate would silently replace one drone's whole path with another's.
        seen: set[int] = set()
        dupes: set[int] = set()
        for d in drones:
            (dupes if d.id in seen else seen).add(d.id)
        if dupes:
            raise ValueError(f"duplicate drone id(s): {sorted(dupes)}")
        return drones


@dataclass
class Show:
    """A full multi-drone choreography.

    Attributes:
        drones: Mapping of drone id to its time-ordered waypoint list.
        version: Show format version (stamped SHOW_FORMAT_VERSION by default).
        geofence: Optional computed or stored geofence envelope dict.
        signature_ref: Optional detached signature manifest filename.
    """

    drones: dict[int, list[dict]] = field(default_factory=dict)
    version: int = SHOW_FORMAT_VERSION
    geofence: dict | None = None
    signature_ref: str | None = None

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

        version = SHOW_FORMAT_VERSION
        geofence = None
        signature_ref = None

        if "drones" in data and isinstance(data["drones"], list):
            version = data.get("version", SHOW_FORMAT_VERSION)
            geofence = data.get("_geofence")
            signature_ref = data.get("signature_ref")
            drones = {}
            for entry in data["drones"]:
                if "id" not in entry:
                    raise ValueError("Studio-shaped drone entry is missing 'id'")
                drones[int(entry["id"])] = list(entry.get("waypoints", []))
        else:
            drones = {int(k): list(v) for k, v in data.items() if not str(k).startswith("_")}
            geofence = data.get("_geofence")

        for drone_id, wps in drones.items():
            for wp in wps:
                missing = [k for k in _REQUIRED_KEYS if k not in wp]
                if missing:
                    raise ValueError(
                        f"drone {drone_id} has a waypoint missing {missing}"
                    )
        return cls(drones=drones, version=version, geofence=geofence, signature_ref=signature_ref)

    @classmethod
    def validate_and_load(cls, data) -> "Show":
        """Build a :class:`Show` from a show file, enforcing the full schema.

        Unlike :meth:`from_any`, only the Studio shape is accepted (it is the
        one that carries ``version``), and every field is type- and
        range-checked against :class:`ShowContainer`.

        Args:
            data: A Studio-shaped dict, e.g. ``json.loads`` of an exported show.

        Raises:
            pydantic.ValidationError: With one entry per bad field in
                ``.errors()``, each carrying its ``loc`` path and message.
        """
        container = ShowContainer.model_validate(data)
        return cls(
            drones={
                track.id: [wp.model_dump(exclude_unset=True) for wp in track.waypoints]
                for track in container.drones
            },
            version=container.version,
            geofence=container.geofence.model_dump(exclude_unset=True) if container.geofence else None,
            signature_ref=container.signature_ref,
        )

    # ------------------------------------------------------------------ dump

    def to_studio(self) -> dict:
        """Return the Studio-shaped dict, including the computed duration and optional metadata."""
        out: dict[str, Any] = {
            "version": self.version,
            "drones": [
                {"id": drone_id, "waypoints": sorted(wps, key=lambda w: w["time"])}
                for drone_id, wps in sorted(self.drones.items())
            ],
            "_duration": self.duration,
        }
        if self.geofence is not None:
            out["_geofence"] = self.geofence
        if self.signature_ref is not None:
            out["signature_ref"] = self.signature_ref
        return out

    def to_gcs(self) -> dict[int, list[dict]]:
        """Return the GCS-shaped ``{drone_id: [waypoints]}`` dict with waypoints ordered by time."""
        return {
            drone_id: sorted(wps, key=lambda w: w["time"])
            for drone_id, wps in sorted(self.drones.items())
        }

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
