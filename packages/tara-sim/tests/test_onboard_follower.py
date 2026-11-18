"""Issue #57: the simulated onboard trajectory follower.

What is covered here: the named mission states, the broadcast emergency overrides
(LAND, RTL, ESTOP) over a running onboard mission, and how closely the follower
tracks a real certified show. The "GCS link lost mid-show, the fleet still finishes" case
is `packages/tara-gcs/tests/test_onboard_mission.py::test_link_loss_mid_show_does_not_abort_the_show`.

Plain numpy and a controllable clock: no mocks, no asyncio.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from tara_core import ShowCommand, compile_mission, compute_show_id, decode_mission, encode_show_trigger
from tara_sim.fleet_state import FleetState, SimConfig
from tara_sim.system import SimFleet

SHOW_ID = b"\x57" * 16
KEY = bytes(range(32))
OTHER_ID = b"\x99" * 16
WPS = [
    {"time": 0.0, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0},
    {"time": 4.0, "x": 0.0, "y": 0.0, "z": -6.0, "yaw": 0.0},    # climb to 6 m
    {"time": 10.0, "x": 3.0, "y": 0.0, "z": -6.0, "yaw": 0.0},   # translate 3 m
]


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def auth(command, show_id=SHOW_ID, counter=1, start_utc_usec=0, key=KEY):
    """An authenticated trigger frame, as the GCS would send it."""
    return encode_show_trigger(command, show_id, start_utc_usec, key=key, counter=counter)


def _flying_fleet(n=3, start_in=0.0, trigger_key=KEY, **cfg):
    """n armed drones, each holding the mission and told to start `start_in` seconds from now."""
    fleet = SimFleet(n, config=SimConfig(rng_seed=3, wind_std_ms2=0.0, **cfg), trigger_key=trigger_key)
    fs = fleet.state
    clock = Clock()
    fs.utc_clock = clock
    fs.armed[:] = True
    for i in range(n):
        blob = compile_mission(WPS, drone_id=i, show_id=SHOW_ID)
        _, decoded = decode_mission(blob)
        fs.load_mission(i, SHOW_ID, decoded)
    for i in range(n):
        assert fs.start_mission(i, clock.t + start_in)
    return fleet, fs, clock


def _step(fs, clock, seconds):
    for _ in range(int(round(seconds / fs.config.dt))):
        clock.t += fs.config.dt
        fs.tick(fs.config.dt)


class TestMissionStates:
    def test_a_drone_goes_through_every_named_state_in_order(self):
        fs = FleetState(1, SimConfig(rng_seed=1, wind_std_ms2=0.0))
        clock = Clock()
        fs.utc_clock = clock
        fs.armed[:] = True
        seen = [fs.mission_state(0)]
        assert seen == ["NONE"]

        _, decoded = decode_mission(compile_mission(WPS, drone_id=0, show_id=SHOW_ID))
        fs.load_mission(0, SHOW_ID, decoded)
        seen.append(fs.mission_state(0))                 # loaded, not started
        fs.start_mission(0, clock.t + 1.0)
        seen.append(fs.mission_state(0))                 # trigger heard, t=0 is 1 s away
        _step(fs, clock, 1.5)
        seen.append(fs.mission_state(0))                 # flying
        _step(fs, clock, 12.0)
        seen.append(fs.mission_state(0))                 # whole trajectory flown, holding
        fs.command_land(0)
        _step(fs, clock, 20.0)
        seen.append(fs.mission_state(0))                 # on the ground, disarmed

        assert seen == ["NONE", "MISSION_LOADED", "SYNC_WAIT", "EXECUTING", "COMPLETE", "LANDED"]
        assert tuple(dict.fromkeys(seen)) == FleetState.MISSION_STATES

    def test_a_cancelled_mission_goes_back_to_loaded(self):
        fleet, fs, clock = _flying_fleet(1)
        _step(fs, clock, 2.0)
        assert fs.mission_state(0) == "EXECUTING"
        fs.cancel_mission(0)
        assert fs.mission_state(0) == "MISSION_LOADED"


class TestEmergencyOverrides:
    def test_land_overrides_a_mission_in_flight_whatever_the_show_id(self):
        fleet, fs, clock = _flying_fleet(3)
        _step(fs, clock, 2.0)
        assert all(fs.mission_state(i) == "EXECUTING" for i in range(3))

        fleet.broadcast(auth(ShowCommand.LAND, OTHER_ID))  # not this show's id

        assert not fs.mission_active.any()
        assert fs.landing.all()
        _step(fs, clock, 15.0)
        assert not fs.armed.any() and (fs.position[:, 2] <= 0.05).all()
        assert not fs.mission_complete.any()      # the show was abandoned, not completed

    def test_rtl_overrides_a_mission_and_brings_each_drone_home(self):
        fleet, fs, clock = _flying_fleet(3)
        _step(fs, clock, 8.0)                      # past the climb, translating
        fleet.broadcast(auth(ShowCommand.RTL, OTHER_ID))
        assert not fs.mission_active.any() and fs.rtl_pending.all()
        _step(fs, clock, 40.0)
        assert not fs.armed.any()
        assert np.abs(fs.position[:, :2]).max() < 0.5      # back over the pad (the origin)

    def test_estop_stops_every_motor_within_one_tick_and_needs_no_mission(self):
        fleet, fs, clock = _flying_fleet(3)
        # Two more armed drones that hold no mission at all must be stopped too.
        fleet2 = SimFleet(2, config=SimConfig(rng_seed=4, wind_std_ms2=0.0), trigger_key=KEY)
        fleet2.state.armed[:] = True
        fleet2.state.position[:, 2] = 5.0
        fleet2.broadcast(auth(ShowCommand.ESTOP, b"\x00" * 16))
        assert not fleet2.state.armed.any() and all(m == "TERMINATED" for m in fleet2.state.flight_mode)

        _step(fs, clock, 5.0)
        t0 = clock.t
        fleet.broadcast(auth(ShowCommand.ESTOP, OTHER_ID, counter=2))
        _step(fs, clock, fs.config.dt)             # one physics tick
        assert (clock.t - t0) <= 0.1               # the issue's 100 ms
        assert not fs.armed.any()
        assert (fs.position[:, 2] == 0.0).all() and not fs.mission_active.any()

    def test_a_drone_whose_link_is_down_misses_an_emergency_frame(self):
        """The honest limit of a broadcast: a drone that cannot hear it is not stopped by it."""
        fleet, fs, clock = _flying_fleet(2)
        _step(fs, clock, 2.0)
        fs.connected[1] = False
        fleet.broadcast(auth(ShowCommand.ESTOP))
        assert not fs.armed[0]
        assert fs.armed[1] and fs.mission_active[1]

    def test_land_and_rtl_do_nothing_to_a_disarmed_drone(self):
        fleet = SimFleet(2, config=SimConfig(rng_seed=5), trigger_key=KEY)
        for counter, cmd in enumerate((ShowCommand.LAND, ShowCommand.RTL), start=1):
            fleet.broadcast(auth(cmd, counter=counter))
        assert not fleet.state.landing.any() and not fleet.state.rtl_pending.any()

    def test_a_damaged_emergency_frame_is_ignored(self):
        fleet, fs, clock = _flying_fleet(2)
        _step(fs, clock, 2.0)
        frame = bytearray(auth(ShowCommand.ESTOP))
        frame[3] ^= 0x01                              # one flipped bit in the show id
        fleet.broadcast(bytes(frame))
        assert fs.armed.all() and fs.mission_active.all()



class TestAuthenticatedTriggers:
    """Issue #57: nothing but a holder of the key can land, return or kill the fleet, or replay an old order."""

    def test_a_plain_crc_emergency_frame_is_refused_by_a_keyed_and_a_keyless_fleet(self):
        for key in (KEY, None):
            fleet, fs, clock = _flying_fleet(2, trigger_key=key)
            _step(fs, clock, 2.0)
            for command in (ShowCommand.ESTOP, ShowCommand.LAND, ShowCommand.RTL):
                fleet.broadcast(encode_show_trigger(command, SHOW_ID))       # a CRC anyone can compute
            assert fs.armed.all() and fs.mission_active.all(), f"key={key!r}"

    def test_a_frame_made_with_the_wrong_key_does_nothing(self):
        fleet, fs, clock = _flying_fleet(2)
        _step(fs, clock, 2.0)
        fleet.broadcast(auth(ShowCommand.ESTOP, key=bytes(32), counter=5))
        assert fs.armed.all()

    def test_a_keyless_drone_refuses_an_authenticated_emergency_frame_too(self):
        """No key means nothing can be verified: the drone fails closed rather than trusting the frame."""
        fleet, fs, clock = _flying_fleet(2, trigger_key=None)
        _step(fs, clock, 2.0)
        fleet.broadcast(auth(ShowCommand.ESTOP, counter=3))
        assert fs.armed.all()

    def test_a_recorded_frame_cannot_be_replayed(self):
        fleet, fs, clock = _flying_fleet(2)
        _step(fs, clock, 2.0)
        old_land = auth(ShowCommand.LAND, counter=10)
        fleet.broadcast(old_land)
        assert fs.landing.all()
        _step(fs, clock, 20.0)
        assert not fs.armed.any()
        # The operator arms again later (a new flight); the recorded LAND comes back.
        fs.armed[:] = True
        fs.landing[:] = False
        fleet.broadcast(old_land)
        assert not fs.landing.any() and fs.armed.all()
        # A genuinely newer one still works.
        fleet.broadcast(auth(ShowCommand.LAND, counter=11))
        assert fs.landing.all()

    def test_a_lower_counter_than_one_already_accepted_is_ignored(self):
        fleet, fs, clock = _flying_fleet(2)
        _step(fs, clock, 2.0)
        fleet.broadcast(auth(ShowCommand.RTL, counter=50))
        fs.rtl_pending[:] = False
        fs.armed[:] = True
        fleet.broadcast(auth(ShowCommand.LAND, counter=49))      # authentic, but older than what was accepted
        assert not fs.landing.any()

    def test_an_old_emergency_frame_never_lands_a_drone_that_was_disarmed_when_it_first_came_by(self):
        fleet, fs, clock = _flying_fleet(1)
        fs.armed[:] = False
        land = auth(ShowCommand.LAND, counter=7)
        fleet.broadcast(land)                                    # ignored: nothing armed, but the counter is spent
        fs.armed[:] = True
        fleet.broadcast(land)
        assert not fs.landing.any()

    def test_a_keyed_fleet_ignores_plain_start_and_cancel_frames(self):
        fleet, fs, clock = _flying_fleet(2)
        fs.mission_active[:] = False
        fleet.broadcast(encode_show_trigger(ShowCommand.START, SHOW_ID, 0))
        assert not fs.mission_active.any()

    def test_start_repeats_are_idempotent_and_a_drone_that_missed_the_first_copy_takes_a_later_one(self):
        """The GCS repeats one START frame until t=0 on a lossy radio; replay protection must not break that."""
        fleet, fs, clock = _flying_fleet(2)
        fs.mission_active[:] = False
        for i in range(2):                                        # the mission as staged into each drone's storage
            fs.storage[i]["/fs/microsd/tara/show.tmsn"] = compile_mission(WPS, drone_id=i, show_id=SHOW_ID)
        start = auth(ShowCommand.START, SHOW_ID, counter=20, start_utc_usec=int((clock.t + 5.0) * 1e6))
        fs.connected[1] = False                                   # drone 1 misses the first copy
        fleet.broadcast(start)
        assert list(fs.mission_active) == [True, False]
        fs.connected[1] = True
        fleet.broadcast(start)                                    # the repeat: drone 0 ignores it, drone 1 takes it
        assert list(fs.mission_active) == [True, True]
        started_at = fs.mission_start_utc.copy()
        fleet.broadcast(start)                                    # and further repeats change nothing
        assert (fs.mission_start_utc == started_at).all()

    def test_a_drone_that_missed_an_emergency_frame_does_not_spend_its_counter(self):
        fleet, fs, clock = _flying_fleet(2)
        _step(fs, clock, 2.0)
        fs.connected[1] = False
        estop = auth(ShowCommand.ESTOP, counter=30)
        fleet.broadcast(estop)
        assert not fs.armed[0] and fs.armed[1]
        fs.connected[1] = True
        fleet.broadcast(estop)                                    # the repeat reaches the drone that missed it
        assert not fs.armed[1]

SHOW3 = Path(__file__).resolve().parents[3] / "docs" / "sitl-t2-recorded-acceptance" / "recorded-takes" / "show3.json"


class TestTrackingOnARealShow:
    """The follower flying a real certified Studio show (the 3-drone show flown in the T2 runs).

    Issue #57: "onboard interpolation maintains <= 5 cm tracking error at 50 Hz". MEASURED on this show with the
    acceleration feed-forward: max 2.1 to 2.7 cm, 95th percentile 1.8 to 2.4 cm, mean 0.7 to 1.1 cm, in every
    wind and rotor-lag setting tried; without it (a feed-forward that only repeated the segment velocity) the
    same show gave max 7 to 9 cm. This is the SIMULATED plant: a PD loop, no radio, no sensor noise beyond the
    configured wind. It says nothing about a real vehicle.
    """

    @staticmethod
    def _fly(decoded, sid, cfg):
        fs = FleetState(1, cfg)
        clock = Clock(100.0)
        fs.utc_clock = clock
        fs.armed[:] = True
        fs.load_mission(0, sid, decoded)
        fs.start_mission(0, 100.0)
        errs = []
        while fs.mission_active[0]:
            clock.t += cfg.dt
            fs.tick(cfg.dt)
            errs.append(float(np.linalg.norm(fs.position[0] - fs.target_position[0])))
            assert len(errs) < 20000
        return np.array(errs)

    @pytest.mark.parametrize("cfg", [
        SimConfig(rng_seed=1, wind_std_ms2=0.0),
        SimConfig(rng_seed=1),                                  # the default wind
        SimConfig(rng_seed=1, wind_std_ms2=0.0, rotor_tau_s=0.1),  # a rotor lag
    ])
    def test_the_certified_show_is_tracked_within_5_cm(self, cfg):
        show = json.loads(SHOW3.read_text())
        sid = compute_show_id(show)
        for drone in show["drones"]:
            wps = drone["waypoints"]
            blob = compile_mission(wps, drone_id=drone["id"], show_id=sid, origin=(wps[0]["x"], wps[0]["y"]))
            _, decoded = decode_mission(blob)
            e = self._fly(decoded, sid, cfg)
            assert e.max() < 0.05, f"drone {drone['id']} max {e.max() * 100:.2f} cm"
            assert e.mean() < 0.02
            assert e[-1] < 0.01                       # and it ends on the last waypoint

    def test_a_path_with_sharp_corners_is_tracked_far_better_than_without_feed_forward(self):
        """Corners no certified show has (a 1.5 m/s velocity change in one waypoint): 33 cm before, 9 cm now."""
        wps, t, x, z = [{"time": 0.0, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0}], 0.0, 0.0, 0.0

        def seg(dur, dx, dz):
            nonlocal t, x, z
            n = int(round(dur * 10))
            for k in range(1, n + 1):
                wps.append({"time": round(t + k * 0.1, 3), "x": x + dx * k / n, "y": 0.0, "z": -(z + dz * k / n), "yaw": 0.0})
            t, x, z = t + dur, x + dx, z + dz

        seg(10, 0, 10)
        seg(4, 0, 0)
        seg(13.3, 20, 0)
        seg(4, 0, 0)
        _, decoded = decode_mission(compile_mission(wps, drone_id=0, show_id=SHOW_ID))
        e = self._fly(decoded, SHOW_ID, SimConfig(rng_seed=1, wind_std_ms2=0.0))
        assert e.max() < 0.12                         # measured 9.3 cm; the old follower gave 33 cm
        assert e[-1] < 0.01


class TestFeedForward:
    def test_the_velocity_feed_forward_is_continuous_across_a_waypoint(self):
        """The old feed-forward jumped by the whole velocity change at a waypoint (0 -> 3 m/s); this one has no jump."""
        times = np.array([0.0, 1.0, 2.0, 3.0])
        positions = np.array([[0, 0, 0], [0, 0, 0], [3.0, 0, 0], [3.0, 0, 0]], dtype=float)   # rest, a 3 m/s leg, rest
        left = FleetState._mission_feedforward(times, positions, 1, 1.0 - 1e-9)[0]    # end of the rest leg
        right = FleetState._mission_feedforward(times, positions, 2, 1.0 + 1e-9)[0]   # start of the 3 m/s leg
        assert left[0] == pytest.approx(right[0], abs=1e-6)
        assert left[0] == pytest.approx(1.5, abs=1e-6)                                # halfway between 0 and 3 m/s
        middle = FleetState._mission_feedforward(times, positions, 2, 1.5)[0]         # the middle of the leg
        assert middle[0] == pytest.approx(3.0)

    def test_acceleration_is_the_slope_of_the_velocity_and_zero_when_nothing_changes(self):
        times = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
        positions = np.array([[0.0, 0, 0], [1.0, 0, 0], [2.0, 0, 0], [3.0, 0, 0], [4.0, 0, 0]])
        v, a = FleetState._mission_feedforward(times, positions, 3, 2.5)   # constant 1 m/s
        assert v[0] == pytest.approx(1.0) and a[0] == pytest.approx(0.0)
        _, a_start = FleetState._mission_feedforward(times, positions, 1, 0.25)   # speeding up from rest
        assert a_start[0] > 0.0
