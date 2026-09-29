"""The show container contract (issue #55).

Every case runs against both halves of the contract — the JSON Schema file
(schemas/show_v1.json) and its Pydantic mirror (tara_core.show.ShowContainer)
— so the two can't quietly drift apart. The only rules that exist on one side
alone are NaN (not representable in JSON) and duplicate drone ids (not
expressible in JSON Schema); those are tested against the model only.
"""

import copy
import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from pydantic_core import ErrorDetails

from tara_core import MAX_FLEET_SIZE, SHOW_FORMAT_VERSION, SHOW_SCHEMA_PATH, Show, ShowContainer


@pytest.fixture(scope="module")
def schema_validator():
    schema = json.loads(SHOW_SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def valid_show() -> dict:
    return {
        "version": SHOW_FORMAT_VERSION,
        "drones": [
            {
                "id": 0,
                "waypoints": [
                    {"time": 0.0, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0},
                    {"time": 1.0, "x": 1.0, "y": 0.0, "z": -2.0, "yaw": 359.9,
                     "r": 255, "g": 128, "b": 0, "w": 0, "vx": 1.0, "vy": 0.0, "vz": -2.0},
                ],
            },
            {"id": 1, "waypoints": [{"time": 0.0, "x": 0.0, "y": 2.0, "z": 0.0, "yaw": 90.0}]},
        ],
    }


def with_wp(**fields) -> dict:
    """valid_show() with drone 0's second waypoint updated (None deletes a key)."""
    show = valid_show()
    wp = show["drones"][0]["waypoints"][1]
    for key, value in fields.items():
        if value is None:
            del wp[key]
        else:
            wp[key] = value
    return show


def model_errors(data) -> list[ErrorDetails]:
    with pytest.raises(ValidationError) as exc:
        ShowContainer.model_validate(data)
    return exc.value.errors()


def schema_error_paths(validator, data) -> list[list]:
    return [list(e.absolute_path) for e in validator.iter_errors(data)]


WP = ["drones", 0, "waypoints", 1]


class TestAccepted:
    def test_valid_show(self, schema_validator):
        schema_validator.validate(valid_show())
        ShowContainer.model_validate(valid_show())

    @pytest.mark.parametrize("extra", [
        {"_geofence": {"envelope_radius_m": 12.0, "soft_warning_radius_m": 15.0, "hard_geofence_radius_m": 20.0}},
        {"signature_ref": "show.json.sig.json"},
        {"signature_ref": None},
        {"_duration": 12.5, "something_main_adds_next_week": {"any": "thing"}},
    ])
    def test_optional_and_unknown_top_level_keys(self, schema_validator, extra):
        show = {**valid_show(), **extra}
        schema_validator.validate(show)
        ShowContainer.model_validate(show)

    def test_future_version_is_accepted(self, schema_validator):
        # Forward compat: a file from a newer writer still validates; what the
        # GCS then does with it is the reader's call (follow-up issue).
        show = {**valid_show(), "version": 99}
        schema_validator.validate(show)
        ShowContainer.model_validate(show)

    def test_drone_id_beyond_old_gcs_cap(self, schema_validator):
        # The GCS parser still caps ids at 99; the schema follows MAX_FLEET_SIZE.
        show = valid_show()
        show["drones"][1]["id"] = 500
        schema_validator.validate(show)
        ShowContainer.model_validate(show)

    def test_unknown_waypoint_and_drone_keys_pass_through(self, schema_validator):
        show = with_wp(label="apex")
        show["drones"][0]["name"] = "lead"
        schema_validator.validate(show)
        loaded = Show.validate_and_load(show)
        assert loaded.drones[0][1]["label"] == "apex"


class TestRejected:
    def test_missing_time(self, schema_validator):
        show = with_wp(time=None)
        assert schema_error_paths(schema_validator, show) == [WP]
        [err] = model_errors(show)
        assert err["loc"] == (*WP, "time")
        assert err["type"] == "missing"

    @pytest.mark.parametrize("yaw", [9999.0, 360.0, -0.1])
    def test_yaw_out_of_range(self, schema_validator, yaw):
        show = with_wp(yaw=yaw)
        assert schema_error_paths(schema_validator, show) == [[*WP, "yaw"]]
        [err] = model_errors(show)
        assert err["loc"] == (*WP, "yaw")

    @pytest.mark.parametrize("field,value", [("r", 300), ("g", -1), ("w", 256), ("b", 12.5), ("r", "255")])
    def test_bad_led_channel(self, schema_validator, field, value):
        show = with_wp(**{field: value})
        assert schema_error_paths(schema_validator, show) == [[*WP, field]]
        [err] = model_errors(show)
        assert err["loc"] == (*WP, field)

    def test_null_led_channel(self, schema_validator):
        # Omitting a channel is fine; an explicit null would crash the .tara compiler.
        show = valid_show()
        show["drones"][0]["waypoints"][1]["r"] = None
        assert schema_error_paths(schema_validator, show) == [[*WP, "r"]]
        [err] = model_errors(show)
        assert err["loc"] == (*WP, "r")

    def test_nan_coordinate(self):
        # NaN isn't valid JSON, so only a Python dict can carry it — model only.
        [err] = model_errors(with_wp(x=float("nan")))
        assert err["loc"] == (*WP, "x")
        assert err["type"] == "finite_number"

    def test_negative_time(self, schema_validator):
        show = with_wp(time=-0.5)
        assert schema_error_paths(schema_validator, show) == [[*WP, "time"]]
        assert model_errors(show)[0]["loc"] == (*WP, "time")

    def test_numeric_string_is_not_a_number(self, schema_validator):
        show = with_wp(z="-2.0")
        assert schema_error_paths(schema_validator, show) == [[*WP, "z"]]
        assert model_errors(show)[0]["loc"] == (*WP, "z")

    def test_missing_version(self, schema_validator):
        show = valid_show()
        del show["version"]
        assert schema_error_paths(schema_validator, show) == [[]]
        [err] = model_errors(show)
        assert err["loc"] == ("version",)
        assert err["type"] == "missing"

    def test_version_zero(self, schema_validator):
        show = {**valid_show(), "version": 0}
        assert schema_error_paths(schema_validator, show) == [["version"]]
        assert model_errors(show)[0]["loc"] == ("version",)

    @pytest.mark.parametrize("drone_id", [-1, MAX_FLEET_SIZE])
    def test_drone_id_out_of_range(self, schema_validator, drone_id):
        show = valid_show()
        show["drones"][1]["id"] = drone_id
        assert schema_error_paths(schema_validator, show) == [["drones", 1, "id"]]
        assert model_errors(show)[0]["loc"] == ("drones", 1, "id")

    def test_incomplete_geofence(self, schema_validator):
        show = {**valid_show(), "_geofence": {"envelope_radius_m": 12.0}}
        # jsonschema reports one "required" error per missing property.
        assert schema_error_paths(schema_validator, show) == [["_geofence"], ["_geofence"]]
        locs = {e["loc"] for e in model_errors(show)}
        assert locs == {("_geofence", "soft_warning_radius_m"), ("_geofence", "hard_geofence_radius_m")}

    def test_duplicate_drone_ids(self):
        # Show keys drones by id, so a duplicate would silently drop a path.
        show = valid_show()
        show["drones"][1]["id"] = 0
        [err] = model_errors(show)
        assert err["loc"] == ("drones",)
        assert "duplicate drone id(s): [0]" in err["msg"]

    def test_every_bad_field_is_reported_not_just_the_first(self):
        show = with_wp(yaw=9999.0, r=300)
        show["drones"][1]["waypoints"][0].pop("time")
        locs = {e["loc"] for e in model_errors(show)}
        assert locs == {(*WP, "yaw"), (*WP, "r"), ("drones", 1, "waypoints", 0, "time")}


class TestSchemaMatchesCode:
    def test_drone_id_bound_tracks_max_fleet_size(self):
        # The schema is static JSON, so this is what forces it to be updated
        # alongside tara_core.layout.MAX_FLEET_SIZE.
        schema = json.loads(SHOW_SCHEMA_PATH.read_text())
        assert schema["$defs"]["drone"]["properties"]["id"]["maximum"] == MAX_FLEET_SIZE - 1


class TestValidateAndLoad:
    def test_returns_the_same_show_as_from_any(self):
        show = valid_show()
        assert Show.validate_and_load(show).drones == Show.from_any(show).drones

    def test_raises_validation_error_not_a_flattened_string(self):
        with pytest.raises(ValidationError) as exc:
            Show.validate_and_load(with_wp(yaw=9999.0))
        assert exc.value.errors()[0]["loc"] == (*WP, "yaw")

    def test_gcs_shape_is_not_a_show_file(self):
        # The GCS shape carries no version, so the strict path refuses it.
        gcs_shape = Show.from_any(valid_show()).to_gcs()
        with pytest.raises(ValidationError):
            Show.validate_and_load(gcs_shape)

    def test_from_any_stays_lenient(self):
        # The strict gate was added alongside from_any, not in place of it:
        # its many internal callers still get the old behaviour.
        lax = copy.deepcopy(with_wp(yaw=9999.0, r=300))
        del lax["version"]
        assert Show.from_any(lax).drones[0][1]["yaw"] == 9999.0
