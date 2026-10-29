"""Launch-pad geometry tests.

The regression here: pads used to sit on a single line and hover altitudes rose
in 0.7 m steps, which is below every safety threshold in the system. A fleet
was in violation while still sitting over its own pads.
"""


import pytest

from tara_core import DEFAULT_LIMITS, SafetyLimits, altitude_layers, launch_grid, min_separation


class TestLaunchGrid:
    @pytest.mark.parametrize("n", [1, 2, 3, 8, 25, 100])
    def test_every_pad_clears_the_design_threshold(self, n):
        pads = launch_grid(n)
        assert len(pads) == n
        positions = {i: (x, y, 0.0) for i, (x, y) in enumerate(pads)}
        if n >= 2:
            assert min_separation(positions) >= DEFAULT_LIMITS.design_separation_m - 1e-9

    def test_default_spacing_is_pad_spacing_m_not_design_separation_m(self):
        """Issue #167: the pad grid has its own, looser constant - pads sit
        still and have no tracking-error margin to absorb, so they can afford
        more room than the in-flight design limit."""
        assert DEFAULT_LIMITS.pad_spacing_m > DEFAULT_LIMITS.design_separation_m
        default_pads = launch_grid(3)
        explicit_pads = launch_grid(3, spacing=DEFAULT_LIMITS.pad_spacing_m)
        assert default_pads == explicit_pads
        design_pads = launch_grid(3, spacing=DEFAULT_LIMITS.design_separation_m)
        assert default_pads != design_pads

    def test_pads_are_unique(self):
        pads = launch_grid(100)
        assert len(set(pads)) == 100

    def test_drone_zero_anchors_the_origin(self):
        assert launch_grid(50)[0] == (0.0, 0.0)

    def test_footprint_grows_as_sqrt_not_linearly(self):
        """100 drones in a line would span ~198 m; a grid must do far better."""
        pads = launch_grid(100)
        span = max(max(x for x, _ in pads), max(y for _, y in pads))
        assert span < 25.0

    def test_empty_fleet(self):
        assert launch_grid(0) == []

    def test_negative_count_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            launch_grid(-1)

    def test_non_positive_spacing_rejected(self):
        with pytest.raises(ValueError, match="spacing must be positive"):
            launch_grid(4, spacing=0.0)

    def test_pad_spacing_tighter_than_design_separation_is_rejected(self):
        with pytest.raises(ValueError, match="pad_spacing_m .* must be >= design_separation_m"):
            SafetyLimits(design_separation_m=2.0, pad_spacing_m=1.9)


class TestAltitudeLayers:
    def test_step_defaults_to_the_design_threshold_not_zero_point_seven(self):
        layers = altitude_layers(3)
        assert layers[1] - layers[0] == pytest.approx(DEFAULT_LIMITS.design_separation_m)
        # The old 0.7 m step violated both the 0.8 m and 1.5 m thresholds.
        assert layers[1] - layers[0] > 0.7

    @pytest.mark.parametrize("n", [1, 10, 100, 1000])
    def test_never_exceeds_the_altitude_ceiling(self, n):
        assert max(altitude_layers(n)) <= DEFAULT_LIMITS.max_altitude_m

    def test_layers_cycle_once_the_ceiling_is_reached(self):
        """A strictly increasing stack would put drone 99 at 200 m."""
        layers = altitude_layers(200)
        assert len(set(layers)) < 200

    def test_drones_sharing_a_layer_are_separated_by_the_grid(self):
        n = 100
        pads = launch_grid(n)
        alts = altitude_layers(n)
        positions = {i: (pads[i][0], pads[i][1], -alts[i]) for i in range(n)}
        assert min_separation(positions) >= DEFAULT_LIMITS.min_separation_m

    def test_negative_count_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            altitude_layers(-1)
