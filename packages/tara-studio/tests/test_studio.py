import pytest
from tara_core import DEFAULT_LIMITS
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
