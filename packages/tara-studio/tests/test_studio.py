import pytest
from tara_core import DEFAULT_LIMITS, MAX_FLEET_SIZE
from tara_studio.generator import generate_circle, generate_helix, generate_heart, generate_cube, generate_spline
from tara_studio.validator import validate_choreography
from tara_studio.exporter import export_kml, export_csv, export_json


class TestPadPositionsAgreeWithGcs:
    """Regression coverage for a real cross-repo bug: the generator used to lay
    every drone out on a 1D line (x=0 always), while tara_gcs.drone_manager's
    _pad_offset uses a 2D grid fixed at MAX_FLEET_SIZE (32 columns). The two
    only coincided by accident for fleets under 32 drones; past that, an
    auto-generated show was silently misplaced once it reached the GCS.
    """

    @pytest.mark.parametrize("n", [5, 32, 33, 60, 100])
    @pytest.mark.parametrize(
        "generate", [generate_circle, generate_helix, generate_heart, generate_cube, generate_spline]
    )
    def test_first_waypoint_matches_gcs_pad_offset(self, n, generate):
        from tara_gcs.drone_manager import _pad_offset

        choreo = generate(n, 30.0, 2.0)
        for drone in choreo["drones"]:
            gcs_x, gcs_y = _pad_offset(drone["id"])
            first = drone["waypoints"][0]
            assert first["x"] == pytest.approx(gcs_x), f"drone {drone['id']} x mismatch at n={n}"
            assert first["y"] == pytest.approx(gcs_y), f"drone {drone['id']} y mismatch at n={n}"

    def test_explicit_spawn_offsets_override_still_works_as_a_flat_y_list(self):
        # Backward compatibility: the Studio UI/API's spawn_offsets is a
        # public, flat list[float] (y-only, x=0) — an explicit override must
        # not be silently reinterpreted as a 2D grid.
        choreo = generate_circle(3, 30.0, 2.0, spawn_offsets=[0.0, 10.0, 20.0])
        xs = [d["waypoints"][0]["x"] for d in choreo["drones"]]
        ys = [d["waypoints"][0]["y"] for d in choreo["drones"]]
        assert xs == [0.0, 0.0, 0.0]
        assert ys == [0.0, 10.0, 20.0]


class TestSpawnOffsetsOverrideAtScale:
    """The manual spawn_offsets override (an operator hand-placing launch pads
    instead of taking the auto-generated grid) was only ever verified up to 32
    drones. Past that it did not fail loudly — it failed as a bare IndexError
    raised from the middle of a shape's waypoint loop whenever the override was
    shorter than num_drones, which the API returned as an opaque 500.
    """

    @pytest.mark.parametrize("n", [33, 50, 64, 100])
    @pytest.mark.parametrize(
        "generate", [generate_circle, generate_helix, generate_heart, generate_cube, generate_spline]
    )
    def test_override_is_honoured_verbatim_above_32_drones(self, n, generate):
        # Hand-placed pads: a y-only flat list, the public override shape.
        offsets = [round(1.5 * i, 1) for i in range(n)]

        choreo = generate(n, 30.0, 2.0, spawn_offsets=offsets)

        assert len(choreo["drones"]) == n
        for drone in choreo["drones"]:
            first = drone["waypoints"][0]
            assert first["x"] == pytest.approx(0.0), f"drone {drone['id']} x at n={n}"
            assert first["y"] == pytest.approx(offsets[drone["id"]]), f"drone {drone['id']} y at n={n}"

    @pytest.mark.parametrize("n", [50, 100])
    def test_override_lands_every_drone_back_on_its_own_overridden_pad(self, n):
        # The override has to survive the whole show, not just the first
        # waypoint: landing returns each drone to start_positions, which is
        # built from the override.
        offsets = [round(2.5 * i, 1) for i in range(n)]

        choreo = generate_circle(n, 30.0, 2.0, spawn_offsets=offsets)

        for drone in choreo["drones"]:
            last = drone["waypoints"][-1]
            assert last["x"] == pytest.approx(0.0, abs=0.01)
            assert last["y"] == pytest.approx(offsets[drone["id"]], abs=0.01)

    @pytest.mark.parametrize("n", [50, MAX_FLEET_SIZE])
    def test_override_accepted_at_the_documented_fleet_ceiling(self, n):
        # Acceptance criterion: overrides work at any drone count up to
        # MAX_FLEET_SIZE. Kept at rate=1.0/duration=10.0 so the largest case
        # stays a geometry check rather than a minute of waypoint generation.
        offsets = [float(i) for i in range(n)]

        choreo = generate_circle(n, 10.0, 1.0, spawn_offsets=offsets)

        assert len(choreo["drones"]) == n
        assert choreo["drones"][-1]["waypoints"][0]["y"] == pytest.approx(offsets[-1])

    @pytest.mark.parametrize("field", ["spawn_offsets", "target_heights"])
    def test_short_override_is_rejected_by_name_not_an_indexerror(self, field):
        # This is the actual >32 failure: a 32-entry override (the old verified
        # ceiling) against a 50-drone show. It used to raise IndexError from
        # deep inside the waypoint loop.
        kwargs = {field: [1.0] * 32}

        with pytest.raises(ValueError, match=field) as excinfo:
            generate_circle(50, 30.0, 2.0, **kwargs)

        # The message has to name both lengths, or an operator can't tell which
        # of the two fields they under-filled.
        assert "32" in str(excinfo.value) and "50" in str(excinfo.value)

    def test_long_override_is_rejected_too(self):
        # Extra entries mean the operator's list is out of sync with the fleet
        # size; silently ignoring the tail would drop pads they placed.
        with pytest.raises(ValueError, match="spawn_offsets"):
            generate_circle(33, 30.0, 2.0, spawn_offsets=[1.0] * 40)

    def test_override_above_max_fleet_size_is_rejected(self):
        n = MAX_FLEET_SIZE + 1
        with pytest.raises(ValueError, match="num_drones"):
            generate_circle(n, 10.0, 1.0, spawn_offsets=[float(i) for i in range(n)])

    def test_auto_grid_path_is_unchanged_at_scale(self):
        # Out of scope for the fix, so pin it: omitting the override still
        # yields the 2D grid that agrees with the GCS, not the flat list.
        from tara_gcs.drone_manager import _pad_offset

        choreo = generate_circle(50, 30.0, 2.0)

        for drone in choreo["drones"]:
            gcs_x, gcs_y = _pad_offset(drone["id"])
            first = drone["waypoints"][0]
            assert first["x"] == pytest.approx(gcs_x)
            assert first["y"] == pytest.approx(gcs_y)


def test_generate_shapes():
    shapes = [
        generate_circle(3, 10.0, 2.0),
        generate_helix(3, 10.0, 2.0),
        generate_heart(3, 10.0, 2.0),
        generate_cube(3, 10.0, 2.0),
        generate_spline(3, 10.0, 2.0)
    ]
    for choreo in shapes:
        assert "drones" in choreo
        assert len(choreo["drones"]) == 3
        assert len(choreo["drones"][0]["waypoints"]) > 0

def test_validator_passed():
    """A comfortably separated show passes at the real default thresholds.

    This used to call generate_circle with safe_dist_threshold=0.5 — a quarter
    of the design limit and a third of the limit the fleet actually aborts on.
    That made the assertion vacuous: it certified a show whose true minimum
    separation was 0.77 m. The threshold override is gone, so this now exercises
    the limits the system really enforces.
    """
    choreo = {
        "drones": [
            {
                "id": d_id,
                "waypoints": [
                    {"time": t * 0.5, "x": 0.0, "y": 6.0 * d_id, "z": -10.0, "yaw": 0.0}
                    for t in range(21)
                ],
            }
            for d_id in range(3)
        ]
    }
    res = validate_choreography(choreo)
    assert res["status"] == "PASSED"
    assert res["metrics"]["min_distance_m"] >= DEFAULT_LIMITS.design_separation_m
    assert len(res["violations"]["proximity"]) == 0


@pytest.mark.parametrize("shape", ["circle", "heart"])
def test_builtin_shapes_are_safe_during_landing(shape):
    """Regression coverage for a real, now-fixed defect.

    The landing phase used to blend horizontal return-to-pad and vertical
    descent simultaneously, driving z = -height * (1 - ratio) while x/y
    converged onto the pad grid at the same time. That let two drones'
    return paths and shrinking altitude gap coincide in space — the circle
    and heart shapes bottomed out around 1.1m, under the 1.5m the fleet
    aborts on, even though the launch-pad grid itself is spaced >=2m apart.

    Fixed in generator.py's _land_waypoint: return to the pad *first*
    (holding altitude), only then descend — so the altitude collapse at
    touchdown always has the pad grid's horizontal separation to fall back
    on. This asserts the fix, at the same drone count/duration that used to
    fail.
    """
    fn = {"circle": generate_circle, "heart": generate_heart}[shape]
    res = validate_choreography(fn(num_drones=3, duration=30.0, rate=2.0))
    assert res["status"] == "PASSED"
    assert res["metrics"]["min_distance_m"] >= DEFAULT_LIMITS.min_separation_m

def test_validator_failed_proximity():
    # Force two drones to occupy exact same coordinate
    choreo = {
        "drones": [
            {
                "id": 0,
                "waypoints": [
                    {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
                    {"time": 5.0, "x": 5.0, "y": 5.0, "z": -2.0, "yaw": 0.0}
                ]
            },
            {
                "id": 1,
                "waypoints": [
                    {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
                    {"time": 5.0, "x": 5.0, "y": 5.0, "z": -2.0, "yaw": 0.0}
                ]
            }
        ]
    }
    res = validate_choreography(choreo, safe_dist_threshold=1.0)
    assert res["status"] == "FAILED"
    assert len(res["violations"]["proximity"]) > 0

def test_validator_failed_speed():
    # Force drone to fly at 50m/s
    choreo = {
        "drones": [
            {
                "id": 0,
                "waypoints": [
                    {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
                    {"time": 1.0, "x": 50.0, "y": 0.0, "z": -2.0, "yaw": 0.0}
                ]
            }
        ]
    }
    res = validate_choreography(choreo, max_velocity=4.0)
    assert res["status"] == "FAILED"
    assert len(res["violations"]["velocity"]) > 0

def test_kml_export():
    choreo = generate_circle(num_drones=2, duration=10.0, rate=2.0)
    kml = export_kml(choreo)
    assert "<kml" in kml
    assert "</kml>" in kml
    assert "Drone 00" in kml
    assert "relativeToGround" in kml

def test_json_csv_export():
    choreo = generate_circle(num_drones=2, duration=10.0, rate=2.0)
    json_out = export_json(choreo)
    csv_out = export_csv(choreo)
    
    assert "drones" in json_out
    assert "waypoints" in json_out
    assert "drone_id,time,x,y,z,yaw" in csv_out
