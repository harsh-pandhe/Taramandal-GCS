"""Tests for the text/SVG/mesh point-cloud voxelizer.

Geometry conventions: coordinates are NED metres, so ``z`` is **negative
upwards** - a formation centred 15 m up sits at ``z = -15.0``. The
``scale_meters`` in a result is the size actually used, which can exceed the
requested one when the fleet does not fit at the requested spacing (see
``TestAutoScale``).
"""

import math

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tara_core import DEFAULT_LIMITS
from tara_studio.server import app
from tara_studio.voxelizer import (
    DEFAULT_MIN_SEPARATION_M,
    MIN_GROUND_CLEARANCE_M,
    bounding_box,
    contour_feasible_scale,
    enforce_min_separation,
    measure_min_separation,
    project_to_plane,
    sample_mesh_point_cloud,
    sample_svg_point_cloud,
    sample_text_point_cloud,
    voxelize,
)

#: The counts the acceptance criteria call out explicitly.
COUNTS = [10, 100, 500, 1000]

#: A closed path exercising every command the issue names: M, L, C, Q and Z.
SVG_PATH_D = "M 0,0 L 100,0 C 150,0 200,50 200,100 Q 200,200 100,200 L 0,200 Z"

SVG_DOCUMENT = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<path d="M 10,10 L 90,10 L 90,90 L 10,90 Z"/>'
    '<path d="M 30,30 L 70,30 C 80,40 80,60 70,70 L 30,70 Z"/>'
    "</svg>"
)


@pytest.fixture(scope="module")
def stl_bytes():
    """A watertight cube exported as binary STL."""
    trimesh = pytest.importorskip("trimesh")
    return trimesh.creation.box(extents=(2.0, 2.0, 2.0)).export(file_type="stl")


@pytest.fixture(scope="module")
def obj_bytes():
    """A subdivided icosphere exported as OBJ - curved, unlike the cube."""
    trimesh = pytest.importorskip("trimesh")
    return trimesh.creation.icosphere(subdivisions=3).export(file_type="obj")


@pytest.fixture(scope="module")
def stl_file(tmp_path_factory, stl_bytes):
    """The cube written to disk, to exercise the load-by-path branch."""
    path = tmp_path_factory.mktemp("meshes") / "cube.stl"
    path.write_bytes(stl_bytes)
    return str(path)


def coords_array(result: dict) -> np.ndarray:
    """The ``coordinates`` list of a voxelize result as an ``(n, 3)`` array."""
    return np.array([[c["x"], c["y"], c["z"]] for c in result["coordinates"]], dtype=float)


class TestSamplingCount:
    """`len(coordinates) == drone_count`, exactly, for every source.

    This is a hard postcondition rather than a nicety: a show is choreographed
    for a specific fleet, so N-1 points means one drone has no waypoint. Mesh
    sampling is where it is genuinely at risk - Poisson-disk radius elimination
    cannot promise a count, and trimesh's own `sample_surface_even` returns 8
    points when asked for 10.
    """

    @pytest.mark.parametrize("n", COUNTS)
    def test_text_sampling_count(self, n):
        result = voxelize("text", "5", n)
        assert len(result["coordinates"]) == n
        assert result["drone_count"] == n

    @pytest.mark.parametrize("n", COUNTS)
    def test_text_sampling_count_multi_glyph(self, n):
        # "INDIA" has 7 contours including the counters of D and A, so the
        # per-contour budget split has to add back up to exactly n.
        result = voxelize("text", "INDIA", n)
        assert len(result["coordinates"]) == n

    @pytest.mark.parametrize("n", COUNTS)
    def test_svg_sampling_count(self, n):
        assert len(voxelize("svg", SVG_PATH_D, n)["coordinates"]) == n

    @pytest.mark.parametrize("n", COUNTS)
    def test_mesh_sampling_count(self, n, stl_bytes):
        result = voxelize("mesh", stl_bytes, n, file_type="stl")
        assert len(result["coordinates"]) == n

    @pytest.mark.parametrize("n", [1, 2, 3, 7])
    def test_tiny_fleets_still_get_exact_counts(self, n):
        # Fewer drones than "INDIA" has contours: the allocator must not hand
        # out one-per-contour and overshoot.
        assert len(voxelize("text", "INDIA", n)["coordinates"]) == n

    def test_ids_are_contiguous_and_ordered(self):
        result = voxelize("text", "5", 50)
        assert [c["id"] for c in result["coordinates"]] == list(range(50))

    def test_samplers_return_requested_count_directly(self, stl_bytes):
        assert sample_text_point_cloud("4", 64).shape == (64, 2)
        assert sample_svg_point_cloud(SVG_PATH_D, 64).shape == (64, 2)
        assert sample_mesh_point_cloud(stl_bytes, 64, file_type="stl").shape == (64, 3)


class TestCoordinateValidity:
    """Every returned coordinate is a finite float."""

    @pytest.mark.parametrize(
        "source_type,content",
        [("text", "5"), ("text", "INDIA"), ("svg", SVG_PATH_D), ("svg", SVG_DOCUMENT)],
    )
    @pytest.mark.parametrize("n", [10, 100, 500])
    def test_no_nan_or_infinite_coords(self, source_type, content, n):
        result = voxelize(source_type, content, n)
        arr = coords_array(result)
        assert arr.shape == (n, 3)
        assert np.all(np.isfinite(arr)), "NaN or inf in generated coordinates"
        for c in result["coordinates"]:
            for axis in ("x", "y", "z"):
                assert isinstance(c[axis], float)
                assert not math.isnan(c[axis]) and not math.isinf(c[axis])

    @pytest.mark.parametrize("n", [10, 100, 500])
    def test_no_nan_or_infinite_coords_mesh(self, n, stl_bytes):
        arr = coords_array(voxelize("mesh", stl_bytes, n, file_type="stl"))
        assert np.all(np.isfinite(arr))

    def test_bounding_box_values_are_finite(self, stl_bytes):
        for result in [
            voxelize("text", "5", 100),
            voxelize("svg", SVG_PATH_D, 100),
            voxelize("mesh", stl_bytes, 100, file_type="stl"),
        ]:
            assert all(math.isfinite(v) for v in result["bounding_box"].values())

    def test_degenerate_geometry_is_rejected_not_silently_nan(self):
        # A zero-length path has no extent to normalise by. The failure mode to
        # avoid is a cloud of NaNs reaching the fleet, so this must raise
        # rather than divide by zero.
        with pytest.raises(ValueError, match="no drawable paths"):
            voxelize("svg", "M 5,5 L 5,5 L 5,5 Z", 10)

    def test_single_point_contour_normalises_without_nan(self):
        # The same degenerate case one layer down: _normalize_2d's zero-extent
        # guard must return finite coordinates rather than 0/0.
        from tara_studio.voxelizer import _normalize_2d

        assert np.all(np.isfinite(_normalize_2d(np.zeros((4, 2)), 20.0)))


class TestMinimumSeparation:
    """No two drones closer than the required floor.

    Note the 0.8 m the issue specifies is *looser* than the platform's real
    limits (`tara_core.safety`: 1.5 m runtime abort, 2.0 m design-time) - 0.8 m
    is the number that module's docstring records as the outdated Studio
    threshold. `test_separation_holds_at_real_flight_limits` therefore also
    pins the stricter case, since that is what a formation intended to fly
    must clear.
    """

    @pytest.mark.parametrize("n", COUNTS)
    def test_minimum_separation_distance(self, n):
        result = voxelize("text", "5", n)
        assert measure_min_separation(coords_array(result)) >= DEFAULT_MIN_SEPARATION_M
        assert result["min_separation_m"] >= DEFAULT_MIN_SEPARATION_M

    @pytest.mark.parametrize(
        "source_type,content", [("text", "INDIA"), ("text", "4"), ("svg", SVG_PATH_D)]
    )
    @pytest.mark.parametrize("n", [100, 500])
    def test_minimum_separation_across_sources(self, source_type, content, n):
        arr = coords_array(voxelize(source_type, content, n))
        assert measure_min_separation(arr) >= DEFAULT_MIN_SEPARATION_M

    @pytest.mark.parametrize("n", [10, 100, 500])
    def test_minimum_separation_mesh(self, n, stl_bytes):
        arr = coords_array(voxelize("mesh", stl_bytes, n, file_type="stl"))
        assert measure_min_separation(arr) >= DEFAULT_MIN_SEPARATION_M

    def test_separation_is_checked_pairwise_not_just_neighbours(self):
        # Guard against a cKDTree query that only inspects the nearest
        # neighbour: brute-force every pair and confirm it agrees.
        arr = coords_array(voxelize("text", "8", 120))
        d = np.sqrt(((arr[:, None, :] - arr[None, :, :]) ** 2).sum(axis=2))
        np.fill_diagonal(d, np.inf)
        assert d.min() >= DEFAULT_MIN_SEPARATION_M
        assert d.min() == pytest.approx(measure_min_separation(arr))

    @pytest.mark.parametrize("n", [50, 200])
    def test_separation_holds_at_real_flight_limits(self, n):
        # The threshold a formation must clear to actually be flyable.
        design = DEFAULT_LIMITS.design_separation_m
        result = voxelize("text", "5", n, min_separation_m=design)
        assert measure_min_separation(coords_array(result)) >= design

    def test_enforce_min_separation_preserves_count_and_fixes_coincident_points(self):
        # Ten drones stacked on one spot have no separating direction; the
        # relaxation pass must still return ten of them, pushed apart.
        stacked = np.zeros((10, 3))
        out = enforce_min_separation(stacked, 1.0)
        assert out.shape == (10, 3)
        assert measure_min_separation(out) >= 1.0


class TestSvgAndMeshSampling:
    """Ingest real SVG and mesh geometry and check the resulting bounds."""

    def test_svg_and_mesh_sampling(self, stl_bytes):
        svg_result = voxelize("svg", SVG_DOCUMENT, 100, scale_meters=20.0, altitude_meters=15.0)
        mesh_result = voxelize(
            "mesh", stl_bytes, 100, scale_meters=20.0, altitude_meters=15.0, file_type="stl"
        )

        for result in (svg_result, mesh_result):
            bb = result["bounding_box"]
            assert result["status"] == "success"
            assert len(result["coordinates"]) == 100
            assert bb["min_x"] <= bb["max_x"]
            assert bb["min_y"] <= bb["max_y"]
            assert bb["min_z"] <= bb["max_z"]
            assert all(math.isfinite(v) for v in bb.values())
            # NED: the formation is above ground, so z is negative throughout.
            assert bb["max_z"] < 0.0

        # A flat SVG on the XY plane is a billboard: it has height (z spans)
        # but no depth (y is a single plane).
        svg_bb = svg_result["bounding_box"]
        assert svg_bb["min_y"] == pytest.approx(svg_bb["max_y"])
        assert svg_bb["max_z"] - svg_bb["min_z"] > 1.0

        # The cube is a solid, so it occupies all three axes.
        mesh_bb = mesh_result["bounding_box"]
        for lo, hi in [("min_x", "max_x"), ("min_y", "max_y"), ("min_z", "max_z")]:
            assert mesh_bb[hi] - mesh_bb[lo] > 1.0

    def test_svg_document_and_bare_path_both_parse(self):
        assert len(voxelize("svg", SVG_DOCUMENT, 40)["coordinates"]) == 40
        assert len(voxelize("svg", SVG_PATH_D, 40)["coordinates"]) == 40

    def test_svg_is_not_upside_down(self):
        # SVG's Y axis points down and the drone frame's points up. A triangle
        # with its apex at SVG-top must come out with its apex highest, i.e.
        # the sparse end of the cloud at greater altitude (more negative z).
        apex_up = "M 50,0 L 100,100 L 0,100 Z"
        arr = coords_array(voxelize("svg", apex_up, 60, plane="XY"))
        altitude = -arr[:, 2]
        top_half = altitude > (altitude.min() + altitude.max()) / 2.0
        # The apex end is narrow, the base is wide: fewer points up top.
        assert top_half.sum() < (~top_half).sum()

    def test_mesh_loads_from_path_bytes_and_obj(self, stl_file, stl_bytes, obj_bytes):
        from_path = voxelize("mesh", stl_file, 50)
        from_bytes = voxelize("mesh", stl_bytes, 50, file_type="stl")
        from_obj = voxelize("mesh", obj_bytes, 50, file_type="obj")
        for result in (from_path, from_bytes, from_obj):
            assert len(result["coordinates"]) == 50
            assert np.all(np.isfinite(coords_array(result)))

    def test_mesh_sampling_is_deterministic(self, stl_bytes):
        a = voxelize("mesh", stl_bytes, 80, file_type="stl", seed=7)
        b = voxelize("mesh", stl_bytes, 80, file_type="stl", seed=7)
        assert a["coordinates"] == b["coordinates"]

    def test_mesh_points_lie_on_the_surface(self, stl_bytes):
        # Poisson-disk sampling draws from the surface, so a cube's points must
        # sit on one of its faces - not inside the volume.
        pts = sample_mesh_point_cloud(stl_bytes, 200, scale_meters=20.0, file_type="stl")
        half = 10.0  # a 20 m cube centred on the origin
        on_face = np.isclose(np.abs(pts), half, atol=1e-6).any(axis=1)
        assert on_face.all()


class TestScaleAndPlacement:
    """Scale, altitude and plane placement."""

    def test_scale_is_respected_when_the_fleet_fits(self):
        # 100 drones fit on a 20 m glyph, so no auto-scaling should kick in and
        # the formation should measure the requested size.
        result = voxelize("text", "5", 100, scale_meters=20.0)
        assert result["scale_meters"] == pytest.approx(20.0)
        bb = result["bounding_box"]
        largest = max(bb["max_x"] - bb["min_x"], bb["max_z"] - bb["min_z"])
        assert largest == pytest.approx(20.0, abs=1.0)

    @pytest.mark.parametrize("scale", [10.0, 40.0, 80.0])
    def test_larger_scale_produces_a_larger_formation(self, scale):
        # Scaling normalises the *largest* extent, and the separation pass can
        # only push points outward, so the figure spans at least the requested
        # size and never dramatically overshoots it.
        bb = voxelize("text", "5", 60, scale_meters=scale, altitude_meters=60.0)["bounding_box"]
        largest = max(bb["max_x"] - bb["min_x"], bb["max_z"] - bb["min_z"])
        assert largest >= scale * 0.9
        assert largest <= scale * 1.5

    def test_scale_is_proportional(self):
        # Doubling the requested scale doubles the formation, whatever the
        # glyph's own aspect ratio happens to be.
        small = voxelize("text", "5", 60, scale_meters=20.0, altitude_meters=60.0)["bounding_box"]
        large = voxelize("text", "5", 60, scale_meters=40.0, altitude_meters=60.0)["bounding_box"]
        small_h = small["max_z"] - small["min_z"]
        large_h = large["max_z"] - large["min_z"]
        assert large_h == pytest.approx(2.0 * small_h, rel=0.1)

    def test_xy_plane_is_a_vertical_billboard(self):
        bb = voxelize("text", "5", 100, plane="XY", altitude_meters=15.0)["bounding_box"]
        # Height varies, depth does not.
        assert bb["max_z"] - bb["min_z"] > 1.0
        assert bb["min_y"] == pytest.approx(bb["max_y"])

    def test_xz_plane_is_horizontal(self):
        result = voxelize("text", "5", 100, plane="XZ", altitude_meters=15.0)
        bb = result["bounding_box"]
        # Flat at one altitude, spread across x and y instead.
        assert bb["min_z"] == pytest.approx(-15.0)
        assert bb["max_z"] == pytest.approx(-15.0)
        assert bb["max_y"] - bb["min_y"] > 1.0

    @pytest.mark.parametrize("altitude", [25.0, 40.0])
    def test_altitude_centres_the_formation(self, altitude):
        # With enough headroom that the ground clamp does not engage, the
        # formation sits centred on the requested altitude.
        arr = coords_array(voxelize("text", "5", 80, plane="XY", altitude_meters=altitude))
        assert -arr[:, 2].mean() == pytest.approx(altitude, abs=2.0)
        assert np.all(arr[:, 2] < 0.0)

    @pytest.mark.parametrize("altitude", [0.0, 2.0, 5.0])
    def test_low_altitude_never_puts_drones_underground(self, altitude):
        # A 20 m figure centred at 5 m would reach to -5 m. Nothing downstream
        # checks ground clearance, so the voxelizer must lift the whole cloud
        # rather than emit coordinates below the ground plane.
        arr = coords_array(
            voxelize("text", "5", 80, plane="XY", scale_meters=20.0, altitude_meters=altitude)
        )
        assert np.all(-arr[:, 2] >= MIN_GROUND_CLEARANCE_M - 1e-6)

    def test_ground_lift_preserves_shape_and_separation(self):
        # The lift is rigid, so the figure's height and its spacing survive it.
        high = voxelize("text", "5", 80, scale_meters=20.0, altitude_meters=40.0)
        low = voxelize("text", "5", 80, scale_meters=20.0, altitude_meters=0.0)
        high_bb, low_bb = high["bounding_box"], low["bounding_box"]
        assert (high_bb["max_z"] - high_bb["min_z"]) == pytest.approx(
            low_bb["max_z"] - low_bb["min_z"], rel=1e-6
        )
        assert low["min_separation_m"] >= DEFAULT_MIN_SEPARATION_M

    def test_plane_is_case_insensitive(self):
        assert voxelize("text", "5", 20, plane="xy")["plane"] == "XY"
        assert voxelize("text", "5", 20, plane="xz")["plane"] == "XZ"

    def test_project_to_plane_accepts_2d_and_3d(self):
        flat = project_to_plane(np.zeros((5, 2)), "XY", 10.0)
        solid = project_to_plane(np.zeros((5, 3)), "XY", 10.0)
        assert flat.shape == (5, 3) and solid.shape == (5, 3)
        assert np.allclose(flat[:, 2], -10.0)

    def test_bounding_box_matches_the_returned_coordinates(self, stl_bytes):
        result = voxelize("mesh", stl_bytes, 120, file_type="stl")
        arr = coords_array(result)
        bb = result["bounding_box"]
        assert bb["min_x"] == pytest.approx(arr[:, 0].min(), abs=1e-3)
        assert bb["max_x"] == pytest.approx(arr[:, 0].max(), abs=1e-3)
        assert bb["min_z"] == pytest.approx(arr[:, 2].min(), abs=1e-3)
        assert bb["max_z"] == pytest.approx(arr[:, 2].max(), abs=1e-3)


class TestAutoScale:
    """Capacity, not preference, sets the floor on formation size.

    An outline of perimeter L holds L / min_separation_m drones and no more.
    Rather than silently deform the shape to force more in, the voxelizer
    computes the honest requirement and grows the formation, reporting both the
    size it used and the size that was asked for.
    """

    def test_large_fleet_on_a_small_glyph_grows_the_formation(self):
        result = voxelize("text", "5", 1000, scale_meters=20.0)
        assert result["requested_scale_meters"] == pytest.approx(20.0)
        assert result["scale_meters"] > 20.0
        assert result["min_separation_m"] >= DEFAULT_MIN_SEPARATION_M

    def test_auto_scale_off_keeps_the_requested_size(self):
        result = voxelize("text", "5", 1000, scale_meters=20.0, auto_scale=False)
        assert result["scale_meters"] == pytest.approx(20.0)
        assert len(result["coordinates"]) == 1000

    def test_small_fleet_is_left_at_the_requested_scale(self):
        result = voxelize("text", "5", 50, scale_meters=20.0)
        assert result["scale_meters"] == pytest.approx(20.0)

    def test_feasible_scale_grows_with_count_and_spacing(self):
        square = [np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])]
        small = contour_feasible_scale(square, 100, 0.8)
        many = contour_feasible_scale(square, 1000, 0.8)
        wider = contour_feasible_scale(square, 100, 2.0)
        assert many == pytest.approx(small * 10.0)
        assert wider == pytest.approx(small * 2.5)

    def test_feasible_scale_matches_the_perimeter_requirement(self):
        # A unit square has perimeter 4 and extent 1, so 40 drones at 0.5 m
        # need 20 m of perimeter, i.e. an extent of 5 m plus the margin.
        square = [np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])]
        assert contour_feasible_scale(square, 40, 0.5) == pytest.approx(5.0 * 1.15)


class TestInputValidation:
    """Bad input raises ValueError rather than returning a broken cloud."""

    @pytest.mark.parametrize("n", [0, -1, -100])
    def test_non_positive_drone_count_is_rejected(self, n):
        with pytest.raises(ValueError, match="drone_count"):
            voxelize("text", "5", n)

    def test_unknown_source_type_is_rejected(self):
        with pytest.raises(ValueError, match="source_type"):
            voxelize("hologram", "5", 10)

    def test_unknown_plane_is_rejected(self):
        with pytest.raises(ValueError, match="plane"):
            voxelize("text", "5", 10, plane="YZ")

    @pytest.mark.parametrize("text", ["", "   ", "\n\t"])
    def test_blank_text_is_rejected(self, text):
        with pytest.raises(ValueError, match="text"):
            voxelize("text", text, 10)

    def test_blank_svg_is_rejected(self):
        with pytest.raises(ValueError, match="svg"):
            voxelize("svg", "   ", 10)

    def test_non_positive_scale_is_rejected(self):
        with pytest.raises(ValueError, match="scale_meters"):
            voxelize("text", "5", 10, scale_meters=0.0)

    def test_raw_mesh_bytes_without_file_type_is_rejected(self, stl_bytes):
        with pytest.raises(ValueError, match="file_type"):
            voxelize("mesh", stl_bytes, 10)

    def test_empty_point_cloud_has_no_bounding_box(self):
        with pytest.raises(ValueError, match="empty"):
            bounding_box(np.empty((0, 3)))


class TestVoxelizeEndpoint:
    """POST /api/voxelize - the API contract the issue specifies."""

    @pytest.fixture(scope="class")
    def client(self):
        return TestClient(app)

    def test_text_request_matches_the_documented_contract(self, client):
        response = client.post(
            "/api/voxelize",
            json={
                "source_type": "text",
                "content": "5",
                "drone_count": 100,
                "scale_meters": 20.0,
                "altitude_meters": 15.0,
                "plane": "XY",
            },
        )
        assert response.status_code == 200
        body = response.json()

        assert body["status"] == "success"
        assert body["drone_count"] == 100
        assert len(body["coordinates"]) == 100

        for key in ("min_x", "max_x", "min_y", "max_y", "min_z", "max_z"):
            assert key in body["bounding_box"]

        first = body["coordinates"][0]
        assert set(first) == {"id", "x", "y", "z"}
        assert first["id"] == 0
        assert [c["id"] for c in body["coordinates"]] == list(range(100))

    @pytest.mark.parametrize("n", COUNTS)
    def test_endpoint_returns_exactly_n_points(self, client, n):
        response = client.post(
            "/api/voxelize",
            json={"source_type": "text", "content": "4", "drone_count": n},
        )
        assert response.status_code == 200
        assert len(response.json()["coordinates"]) == n

    def test_endpoint_svg_source(self, client):
        response = client.post(
            "/api/voxelize",
            json={"source_type": "svg", "content": SVG_PATH_D, "drone_count": 80},
        )
        assert response.status_code == 200
        assert len(response.json()["coordinates"]) == 80

    def test_endpoint_mesh_source_from_path(self, client, stl_file):
        response = client.post(
            "/api/voxelize",
            json={"source_type": "mesh", "content": stl_file, "drone_count": 80},
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["coordinates"]) == 80
        assert body["min_separation_m"] >= DEFAULT_MIN_SEPARATION_M

    def test_endpoint_coordinates_are_json_safe(self, client):
        # NaN/inf would serialise to bare NaN/Infinity tokens, which strict
        # JSON parsers reject - so the response text must not contain them.
        response = client.post(
            "/api/voxelize",
            json={"source_type": "text", "content": "INDIA", "drone_count": 200},
        )
        assert response.status_code == 200
        assert "NaN" not in response.text and "Infinity" not in response.text

    def test_endpoint_rejects_unknown_source_type(self, client):
        response = client.post(
            "/api/voxelize",
            json={"source_type": "hologram", "content": "5", "drone_count": 10},
        )
        assert response.status_code == 400
        assert "source_type" in response.json()["detail"]

    def test_endpoint_rejects_unknown_plane(self, client):
        response = client.post(
            "/api/voxelize",
            json={"source_type": "text", "content": "5", "drone_count": 10, "plane": "YZ"},
        )
        assert response.status_code == 400

    def test_endpoint_rejects_missing_mesh_file(self, client):
        response = client.post(
            "/api/voxelize",
            json={"source_type": "mesh", "content": "no_such_model.stl", "drone_count": 10},
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("n", [0, -5])
    def test_endpoint_rejects_non_positive_count(self, client, n):
        response = client.post(
            "/api/voxelize",
            json={"source_type": "text", "content": "5", "drone_count": n},
        )
        assert response.status_code == 422  # rejected by the Pydantic bound

    def test_endpoint_honours_the_real_flight_separation(self, client):
        design = DEFAULT_LIMITS.design_separation_m
        response = client.post(
            "/api/voxelize",
            json={
                "source_type": "text",
                "content": "5",
                "drone_count": 100,
                "min_separation_m": design,
            },
        )
        assert response.status_code == 200
        assert response.json()["min_separation_m"] >= design
