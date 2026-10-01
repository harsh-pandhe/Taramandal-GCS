"""Issue #54: aerodynamic power and battery endurance validation.

The physics tests check the momentum-theory solver against its closed forms.
The show-level tests build shows whose energy is easy to reason about (take
off, hover for T seconds, land), so that "fails", "passes" and "cut this much
and it passes" are statements about arithmetic, not snapshots of whatever the
model happened to print.
"""

import json
import math
import time

import pytest
from fastapi import HTTPException

from tara_studio import server
from tara_studio.energy import (
    DEFAULT_AIRFRAME,
    DEFAULT_BATTERY,
    AirframeSpec,
    BatterySpec,
    Environment,
    EnduranceLimits,
    air_density,
    hover_power_w,
    induced_velocity,
    validate_energy,
)
from tara_studio.generator import generate_circle, generate_helix


def _hover_show(hover_s: float, altitude_m: float = 10.0, n: int = 1) -> dict:
    """Climb for 5s, hover for ``hover_s``, descend for 5s, on ``n`` pads 5m apart."""
    drones = []
    for i in range(n):
        y = 5.0 * i
        drones.append({"id": i, "waypoints": [
            {"time": 0.0, "x": 0.0, "y": y, "z": 0.0},
            {"time": 5.0, "x": 0.0, "y": y, "z": -altitude_m},
            {"time": 5.0 + hover_s, "x": 0.0, "y": y, "z": -altitude_m},
            {"time": 10.0 + hover_s, "x": 0.0, "y": y, "z": 0.0},
        ]})
    return {"drones": drones}


class TestMomentumTheory:
    def test_air_density_matches_the_standard_atmosphere(self):
        assert air_density(15.0) == pytest.approx(1.225, abs=0.001)
        assert air_density(-10.0) > air_density(15.0) > air_density(35.0)
        # ISA at 2000m: 79.5kPa and 2.0C.
        assert air_density(2.0, 2000.0) == pytest.approx(1.007, abs=0.002)

    def test_hover_induced_velocity_is_the_ideal_value(self):
        thrust, rho, area = 19.6, 1.225, 0.2
        v_h = math.sqrt(thrust / (2 * rho * area))
        assert float(induced_velocity(thrust, rho, area, 0.0, 0.0)) == pytest.approx(v_h)

    def test_axial_climb_matches_the_closed_form(self):
        thrust, rho, area, climb = 19.6, 1.225, 0.2, 3.0
        v_h = math.sqrt(thrust / (2 * rho * area))
        expected = -climb / 2 + math.sqrt((climb / 2) ** 2 + v_h ** 2)
        assert float(induced_velocity(thrust, rho, area, climb, 0.0)) == pytest.approx(expected, rel=1e-9)

    def test_fast_forward_flight_approaches_glauerts_limit(self):
        """For V >> v_h, v_i -> v_h^2 / V."""
        thrust, rho, area, speed = 19.6, 1.225, 0.2, 40.0
        v_h2 = thrust / (2 * rho * area)
        assert float(induced_velocity(thrust, rho, area, 0.0, speed)) == pytest.approx(v_h2 / speed, rel=0.01)

    def test_descent_is_costed_like_hover_not_cheaper(self):
        """Vortex-ring state makes real descent power higher than momentum
        theory's prediction, so the model refuses to credit it."""
        hover = float(induced_velocity(19.6, 1.225, 0.2, 0.0, 0.0))
        assert float(induced_velocity(19.6, 1.225, 0.2, -2.0, 0.0)) == pytest.approx(hover)

    def test_default_airframe_hovers_on_a_plausible_power(self):
        """X500-class, 2kg on 10-inch props: a couple of hundred watts."""
        assert 150.0 < hover_power_w() < 300.0

    def test_thin_air_and_heavier_airframes_cost_more_to_hover(self):
        assert hover_power_w(env=Environment(venue_altitude_m=2500.0)) > hover_power_w()
        assert hover_power_w(AirframeSpec(mass_kg=3.0)) > hover_power_w()


class TestBatteryDischarge:
    def test_a_parked_drone_only_pays_for_its_avionics(self):
        parked = {"drones": [{"id": 0, "waypoints": [
            {"time": 0.0, "x": 0.0, "y": 0.0, "z": 0.0},
            {"time": 360.0, "x": 0.0, "y": 0.0, "z": 0.0},
        ]}]}
        report = validate_energy(parked)
        # 10W for 6 minutes is 1Wh, within a sample's rounding.
        assert report["metrics"]["total_energy_wh"] == pytest.approx(
            DEFAULT_AIRFRAME.avionics_power_w * 360.0 / 3600.0, rel=0.01
        )

    def test_energy_is_power_times_time_and_current_reflects_voltage_sag(self):
        hover_s = 300.0
        report = validate_energy(_hover_show(hover_s))
        metrics = report["metrics"]
        # Most of the show is hover, so energy is close to hover power x time.
        assert metrics["total_energy_wh"] == pytest.approx(
            metrics["hover_power_w"] * (hover_s + 10.0) / 3600.0, rel=0.1
        )
        # Current is at least what the full-charge open-circuit voltage would
        # need (4S x 4.2V); sag only ever pushes it up.
        assert metrics["peak_current_a"] > metrics["hover_power_w"] / (4 * 4.2)

    def test_the_battery_timeline_only_ever_goes_down(self):
        timeline = validate_energy(_hover_show(120.0))["timeline"]
        remaining = timeline["min_remaining_pct"]
        assert remaining[0] > 99.0
        assert all(b <= a for a, b in zip(remaining, remaining[1:]))
        assert len(timeline["time_s"]) == len(remaining) == len(timeline["max_current_a"])

    def test_cold_weather_shrinks_the_usable_pack(self):
        warm = validate_energy(_hover_show(600.0))
        cold = validate_energy(_hover_show(600.0), environment=Environment(ambient_temp_c=-10.0))
        assert cold["metrics"]["usable_capacity_mah"] < warm["metrics"]["usable_capacity_mah"]
        assert cold["metrics"]["max_depth_of_discharge_pct"] > warm["metrics"]["max_depth_of_discharge_pct"]

    def test_liion_packs_are_supported(self):
        report = validate_energy(_hover_show(60.0), battery=BatterySpec(chemistry="liion", capacity_mah=10000))
        assert report["status"] == "PASSED"
        assert report["limits"]["min_cell_voltage_v"] == 3.0

    def test_unknown_chemistry_is_rejected(self):
        with pytest.raises(ValueError, match="unknown battery chemistry"):
            BatterySpec(chemistry="nicd")


class TestEnduranceVerdict:
    def test_a_short_show_passes(self):
        report = validate_energy(_hover_show(120.0))
        assert report["status"] == "PASSED"
        assert report["metrics"]["required_duration_reduction_s"] == 0
        assert not any(report["violations"].values())

    def test_a_show_that_drains_past_70_percent_is_unsafe_and_says_how_much_to_cut(self):
        report = validate_energy(_hover_show(1200.0))

        assert report["status"] == "FAILED"
        assert report["summary"].startswith("UNSAFE")
        [violation] = report["violations"]["depth_of_discharge"]
        assert violation["depth_of_discharge_pct"] > 70.0
        assert violation["limit_pct"] == 70.0
        cut = report["metrics"]["required_duration_reduction_s"]
        assert cut == violation["required_reduction_s"] > 0
        assert f"shorten the show by at least {cut}s" in report["summary"]
        assert report["metrics"]["max_safe_duration_s"] == pytest.approx(report["metrics"]["duration_s"] - cut)

    def test_cutting_the_reported_amount_makes_the_show_pass(self):
        """The acceptance criterion's "required duration reduction" has to be
        a number a designer can act on: cut that much and the show is safe."""
        cut = validate_energy(_hover_show(1200.0))["metrics"]["required_duration_reduction_s"]
        assert validate_energy(_hover_show(1200.0 - cut))["status"] == "PASSED"
        # And it is not wildly generous: half a minute less cut still fails.
        assert validate_energy(_hover_show(1200.0 - cut + 30.0))["status"] == "FAILED"

    def test_the_depth_of_discharge_limit_is_configurable(self):
        show = _hover_show(600.0)
        assert validate_energy(show)["status"] == "PASSED"
        assert validate_energy(show, limits=EnduranceLimits(max_depth_of_discharge=0.3))["status"] == "FAILED"

    def test_a_pack_too_small_for_the_airframe_breaks_its_c_rating(self):
        report = validate_energy(_hover_show(30.0), battery=BatterySpec(capacity_mah=500))
        [violation] = report["violations"]["c_rate"]
        assert violation["c_rate"] > DEFAULT_BATTERY.spec.max_c_rate
        assert report["metrics"]["peak_c_rate"] > 25.0
        assert report["status"] == "FAILED"

    def test_heavy_sag_below_the_failsafe_voltage_is_flagged(self):
        """A high-resistance pack sags under hover load before it's anywhere
        near empty: the flight controller would force a landing anyway."""
        report = validate_energy(
            _hover_show(30.0), battery=BatterySpec(internal_resistance_ohm=0.1)
        )
        [violation] = report["violations"]["voltage"]
        assert violation["cell_voltage_v"] < 3.5
        assert report["violations"]["depth_of_discharge"] == []
        assert "failsafe floor" in report["summary"]

    def test_the_worst_drone_is_the_one_reported(self):
        show = _hover_show(300.0, n=3)
        # Drone 2 flies the same hover twice as high: more climb energy.
        for wp in show["drones"][2]["waypoints"]:
            wp["z"] *= 2
        report = validate_energy(show)
        assert report["metrics"]["worst_drone"] == 2
        assert report["metrics"]["max_drone_energy_wh"] * 3 > report["metrics"]["total_energy_wh"]


class TestReportShape:
    def test_generated_shows_validate_and_serialize(self):
        report = validate_energy(generate_helix(10, 60.0, 2.0))
        assert report["status"] == "PASSED"
        for key in ("total_energy_wh", "peak_current_a", "peak_c_rate", "min_remaining_pct"):
            assert report["metrics"][key] > 0
        json.dumps(report)  # FastAPI must be able to send it as-is

    def test_the_timeline_is_bounded_for_the_ui(self):
        timeline = validate_energy(_hover_show(1200.0))["timeline"]
        assert len(timeline["time_s"]) <= 401
        assert timeline["time_s"][-1] == pytest.approx(1210.0)

    def test_an_empty_show_fails(self):
        assert validate_energy({"drones": []})["status"] == "FAILED"

    def test_a_large_fleet_stays_fast(self):
        show = generate_circle(300, 60.0, 2.0)
        start = time.monotonic()
        report = validate_energy(show)
        assert time.monotonic() - start < 10.0
        assert report["metrics"]["peak_current_drone"] in range(300)


class TestEndpoint:
    def test_validate_energy_endpoint_reports_energy_and_current(self):
        report = server.api_validate_energy(server.EnergyRequest(choreo=_hover_show(60.0)))
        assert report["status"] == "PASSED"
        assert report["metrics"]["total_energy_wh"] > 0
        assert report["metrics"]["peak_current_a"] > 0

    def test_request_battery_settings_reach_the_model(self):
        cold_small = server.api_validate_energy(server.EnergyRequest(
            choreo=_hover_show(600.0), capacity_mah=3000, ambient_temp_c=-10.0,
        ))
        assert cold_small["status"] == "FAILED"
        assert cold_small["metrics"]["usable_capacity_mah"] == pytest.approx(3000 * 0.72, abs=1)

    def test_a_bad_chemistry_is_a_400(self):
        with pytest.raises(HTTPException) as exc:
            server.api_validate_energy(server.EnergyRequest(choreo=_hover_show(60.0), chemistry="nicd"))
        assert exc.value.status_code == 400
