"""The fleet RF protocol's numbers are the whole point, so they get pinned here.

Every figure this suite asserts traces back to the same physical constraint: a
915MHz link that carries ~250kbps shared across the entire fleet. The three
headline tests (packet size, slot disjointness, bandwidth) are the acceptance
criteria; the rest guard the codecs and the slot arithmetic they rest on,
because a frame that packs to the right *size* while decoding to the wrong
*values* would satisfy the budget and still fly the show wrong.
"""

import struct

import pytest

from tara_gcs.broadcast_manager import (
    AUTH_TAG_SIZE,
    BROADCAST_SYSID,
    DOWNLINK_HZ,
    FRAME_SIZE,
    MAC_FRAMING_BYTES,
    PAYLOAD_SIZE,
    BroadcastManager,
    BroadcastReceiver,
    FrameStatus,
    SwarmState,
    decode_broadcast_frame,
    downlink_bitrate_bps,
    encode_broadcast_frame,
    generate_show_key,
    sync_checksum,
)
from tara_gcs.tdma_scheduler import (
    EPOCH_SECONDS,
    RADIO_CAPACITY_BPS,
    STATUS_FRAME_SIZE,
    STATUS_PAYLOAD_SIZE,
    TDMAScheduler,
    channel_utilization,
    decode_status_payload,
    encode_status_payload,
    total_bitrate_bps,
)

FLEET = 500

#: Fixed so the codec tests are deterministic; the auth tests also use fresh keys.
KEY = bytes(range(32))


class TestDownlinkPacketSize:
    """Acceptance criterion: the broadcast frame fits in 52 bytes on the wire."""

    def test_downlink_packet_size(self):
        payload = encode_broadcast_frame(
            KEY,
            utc_timestamp_usec=1_700_000_000_000_000,
            state=SwarmState.RUN,
            sequence_id=42,
            frame_counter=1,
        )
        framed = len(payload) + MAC_FRAMING_BYTES
        assert framed <= 52, f"broadcast frame is {framed} bytes, budget is 52"
        assert framed == FRAME_SIZE

    def test_payload_is_exactly_the_specified_size(self):
        payload = encode_broadcast_frame(KEY, 0, SwarmState.IDLE, 0, 0)
        assert len(payload) == PAYLOAD_SIZE == 32

    def test_size_is_independent_of_field_values(self):
        # Fixed-width by construction — a max-value frame must not be larger
        # than an all-zero one, or the budget would hold only on a good day.
        biggest = encode_broadcast_frame(KEY, 2**64 - 1, SwarmState.ESTOP, 2**32 - 1, 2**64 - 1)
        smallest = encode_broadcast_frame(KEY, 0, SwarmState.IDLE, 0, 0)
        assert len(biggest) == len(smallest) == PAYLOAD_SIZE

    def test_downlink_cost_does_not_scale_with_fleet_size(self):
        # The core claim of the asymmetric design: one broadcast serves 500
        # drones for the same 3.68kbps it costs to serve one.
        assert downlink_bitrate_bps() == FRAME_SIZE * 8 * DOWNLINK_HZ
        assert downlink_bitrate_bps() < 4_000


class TestTDMASlotAssignment:
    """Acceptance criterion: 500 drones get disjoint slots across 2.0s."""

    def test_tdma_slot_assignment(self):
        sched = TDMAScheduler(fleet_size=FLEET)
        slots = sched.all_slots()

        assert len(slots) == FLEET
        assert {s.drone_id for s in slots} == set(range(FLEET))

        # Disjoint and inside the epoch. Checked pairwise-by-adjacency after
        # sorting rather than with an O(N^2) sweep: slots are intervals on a
        # line, so non-overlap of every neighbouring pair is exactly non-overlap
        # of every pair.
        ordered = sorted(slots, key=lambda s: s.start_s)
        for earlier, later in zip(ordered, ordered[1:]):
            assert not earlier.overlaps(later)
            assert earlier.end_s <= later.start_s

        assert ordered[0].start_s == 0.0
        assert ordered[-1].end_s == pytest.approx(EPOCH_SECONDS)

    def test_slots_cover_the_epoch_with_no_dead_air(self):
        sched = TDMAScheduler(fleet_size=FLEET)
        covered = sum(s.width_s for s in sched.all_slots())
        assert covered == pytest.approx(EPOCH_SECONDS)

    def test_slot_width_is_4ms_at_500_drones(self):
        assert TDMAScheduler(fleet_size=FLEET).slot_width_s == pytest.approx(0.004)

    def test_every_instant_belongs_to_exactly_one_drone(self):
        # The inverse mapping has to agree with the forward one, or a GCS would
        # blame the wrong drone for an out-of-slot transmission.
        sched = TDMAScheduler(fleet_size=FLEET)
        for drone_id in (0, 1, 249, 498, 499):
            slot = sched.slot_for(drone_id)
            midpoint = (slot.start_s + slot.end_s) / 2
            assert sched.drone_for_offset(midpoint) == drone_id
            assert sched.drone_for_offset(slot.start_s) == drone_id
            assert slot.contains(midpoint)

    def test_slot_boundary_belongs_to_the_later_slot(self):
        # Half-open intervals: the shared instant is the next drone's, so the
        # two never both consider it theirs.
        sched = TDMAScheduler(fleet_size=FLEET)
        boundary = sched.slot_for(0).end_s
        assert not sched.slot_for(0).contains(boundary)
        assert sched.slot_for(1).contains(boundary)

    @pytest.mark.parametrize("fleet_size", [2, 3, 7, 100, 499, 500, 997, 1000])
    def test_slot_boundaries_are_exact_not_merely_close(self, fleet_size):
        # Regression guard with teeth. Computing a slot as `start + width`
        # instead of `(i + 1) * epoch / N` overshoots the next slot's start by
        # ~7e-18 on 22 of the 499 boundaries at N=500 — a real overlap that
        # pytest.approx would happily wave through. Exact equality is the
        # assertion that catches it, checked on every boundary rather than a
        # sampled few, since which ones drift depends on the fleet size.
        sched = TDMAScheduler(fleet_size=fleet_size)
        slots = sched.all_slots()
        for earlier, later in zip(slots, slots[1:]):
            assert earlier.end_s == later.start_s
        assert slots[0].start_s == 0.0
        assert slots[-1].end_s == EPOCH_SECONDS

    def test_a_drone_slot_does_not_move_when_queried_again(self):
        # Stateless assignment: a rebooting drone must rejoin on the slot it
        # left, which only holds if the answer is pure arithmetic.
        sched = TDMAScheduler(fleet_size=FLEET)
        assert sched.slot_for(311).start_s == sched.slot_for(311).start_s
        assert sched.slot_for(311).start_s == 311 * sched.slot_width_s

    def test_frame_fits_comfortably_inside_its_slot(self):
        # Headroom is what absorbs clock drift between drones; a slot the frame
        # nearly fills would let a fast clock bleed into a neighbour.
        sched = TDMAScheduler(fleet_size=FLEET)
        assert sched.transmission_time_s() < sched.slot_width_s
        assert sched.slot_utilization() < 0.5

    def test_rejects_out_of_fleet_drone_ids(self):
        sched = TDMAScheduler(fleet_size=FLEET)
        with pytest.raises(ValueError):
            sched.slot_for(FLEET)
        with pytest.raises(ValueError):
            sched.slot_for(-1)

    def test_rejects_a_nonsensical_fleet(self):
        with pytest.raises(ValueError):
            TDMAScheduler(fleet_size=0)


class TestBandwidthUtilization:
    """Acceptance criterion: 500 drones fit in 80kbps, total."""

    def test_bandwidth_utilization(self):
        total_bps = total_bitrate_bps(FLEET)
        total_kbps = total_bps / 1000
        assert total_kbps <= 80, f"protocol needs {total_kbps:.2f}kbps, budget is 80"

        # Where it goes: a fixed downlink plus an uplink that scales with N.
        sched = TDMAScheduler(fleet_size=FLEET)
        assert total_bps == pytest.approx(
            downlink_bitrate_bps() + sched.uplink_bitrate_bps()
        )

    def test_stays_under_30_percent_of_the_radio(self):
        # The objective in the issue: channel utilization under 30%, versus the
        # ~3200% that 10Hz unicast telemetry to 500 drones would demand.
        assert channel_utilization(FLEET) < 0.30

    def test_beats_the_unicast_design_it_replaces(self):
        # 10Hz unicast to 500 drones, at two payload sizes: a hypothetical
        # 52-byte minimal frame, and the ~200-byte full MAVLink telemetry frame
        # the issue's 8Mbps figure is drawn from. Both swamp the link — the
        # smaller one by 11x, the realistic one by 34x — which is the CSMA/CA
        # collapse this protocol exists to avoid.
        for payload_bytes, expected_mbps in ((52, 2.64), (200, 8.56)):
            naive_bps = FLEET * (payload_bytes + MAC_FRAMING_BYTES) * 8 * 10
            assert naive_bps / 1e6 == pytest.approx(expected_mbps, abs=0.01)
            assert naive_bps > RADIO_CAPACITY_BPS

        realistic_unicast = FLEET * (200 + MAC_FRAMING_BYTES) * 8 * 10
        assert total_bitrate_bps(FLEET) < realistic_unicast / 100

    def test_uplink_scales_linearly_with_fleet_size(self):
        one = TDMAScheduler(fleet_size=1).uplink_bitrate_bps()
        many = TDMAScheduler(fleet_size=FLEET).uplink_bitrate_bps()
        assert many == pytest.approx(one * FLEET)

    def test_budget_holds_at_the_1000_drone_ceiling(self):
        # MAX_FLEET_SIZE is 1000; the protocol should not be quietly tuned to
        # only survive the 500 the acceptance criteria happen to name.
        assert channel_utilization(1000) < 0.60


class TestBroadcastFrameCodec:
    def test_round_trips_every_field(self):
        payload = encode_broadcast_frame(KEY, 1_700_000_000_123_456, SwarmState.HOLD, 7, 99)
        decoded = decode_broadcast_frame(payload, KEY)
        assert decoded["utc_timestamp_usec"] == 1_700_000_000_123_456
        assert decoded["state"] == SwarmState.HOLD
        assert decoded["sequence_id"] == 7
        assert decoded["frame_counter"] == 99
        assert decoded["valid"]

    def test_round_trips_every_swarm_state(self):
        for state in SwarmState:
            decoded = decode_broadcast_frame(encode_broadcast_frame(KEY, 0, state, 0, 0), KEY)
            assert decoded["state"] == state
            assert decoded["valid"]

    def test_swarm_state_values_match_the_wire_contract(self):
        # Drones parse these as bare integers; renumbering would silently turn
        # an ESTOP into an RTL.
        assert (SwarmState.IDLE, SwarmState.ARMED, SwarmState.RUN) == (0, 1, 2)
        assert (SwarmState.HOLD, SwarmState.RTL, SwarmState.ESTOP) == (3, 4, 5)

    def test_layout_is_packed_little_endian_with_no_alignment_padding(self):
        # Pinned explicitly: a drone's C parser reads these offsets literally,
        # so struct's default alignment sneaking in a pad byte would desync the
        # whole fleet rather than fail loudly.
        payload = encode_broadcast_frame(
            KEY, 0xDEADBEEFCAFEF00D, SwarmState.RUN, 0x11223344, 0x0102030405060708
        )
        assert struct.unpack_from("<Q", payload, 0)[0] == 0xDEADBEEFCAFEF00D
        assert struct.unpack_from("<B", payload, 8)[0] == int(SwarmState.RUN)
        assert struct.unpack_from("<I", payload, 9)[0] == 0x11223344
        assert struct.unpack_from("<Q", payload, 15)[0] == 0x0102030405060708

    def test_reserved_byte_is_zero_filled(self):
        payload = encode_broadcast_frame(KEY, 2**64 - 1, SwarmState.ESTOP, 2**32 - 1, 2**64 - 1)
        assert payload[23] == 0

    def test_tag_is_the_last_eight_bytes(self):
        payload = encode_broadcast_frame(KEY, 1, SwarmState.RUN, 2, 3)
        assert AUTH_TAG_SIZE == 8
        assert payload[24:] != bytes(8)

    def test_known_answer_vector(self):
        # Pinned so drone firmware can check its HMAC construction against a
        # fixed input: HMAC-SHA256(key, b"TARA-BCAST\x01" + payload[0:24])[:8].
        payload = encode_broadcast_frame(
            KEY, 1_700_000_000_000_000, SwarmState.ESTOP, 7, 1_700_000_000_000_000
        )
        assert payload.hex() == (
            "00401e18240a06000507000000546200401e18240a060000" "8cf18f23eb68c946"
        )

    def test_checksum_catches_a_flip_in_the_high_half_of_the_timestamp(self):
        # A plain 16-bit truncation would miss this entirely.
        base = 1_700_000_000_000_000
        assert sync_checksum(base, 2, 1) != sync_checksum(base ^ (1 << 40), 2, 1)

    def test_checksum_is_16_bit(self):
        assert 0 <= sync_checksum(2**64 - 1, 255, 2**32 - 1) <= 0xFFFF

    def test_rejects_a_wrong_sized_payload(self):
        with pytest.raises(ValueError):
            decode_broadcast_frame(b"\x00" * 16, KEY)

    def test_rejects_a_short_key(self):
        with pytest.raises(ValueError):
            encode_broadcast_frame(b"too-short", 0, SwarmState.IDLE, 0, 0)
        with pytest.raises(ValueError):
            BroadcastManager(b"")


class TestBroadcastAuthentication:
    """Issue #50 acceptance criteria: authenticated frames and anti-replay."""

    NOW = 1_700_000_000.0

    def _pair(self, key=None):
        key = key or generate_show_key()
        clock = [self.NOW]
        mgr = BroadcastManager(key, clock=lambda: clock[0])
        rx = BroadcastReceiver(key, clock=lambda: clock[0])
        return mgr, rx, clock

    def test_valid_hmac_frames_decode_with_status_ok(self):
        mgr, rx, clock = self._pair()
        mgr.set_state(SwarmState.RUN)
        for _ in range(20):
            frame = rx.receive(mgr.tick())
            assert frame["status"] is FrameStatus.OK
            assert frame["state"] == SwarmState.RUN
            clock[0] += 1 / DOWNLINK_HZ

    def test_tampered_state_byte_fails_hmac_and_is_dropped(self):
        mgr, rx, _ = self._pair()
        mgr.set_state(SwarmState.RUN)
        rx.receive(mgr.tick())
        mgr.set_state(SwarmState.HOLD)
        forged = bytearray(mgr.tick())
        forged[8] = int(SwarmState.ESTOP)

        frame = rx.receive(bytes(forged))

        assert frame["status"] is FrameStatus.BAD_TAG
        assert not frame["valid"]
        # Dropped: the receiver still acts on the last good frame.
        assert rx.last_frame["state"] == SwarmState.RUN

    def test_tamperer_who_fixes_up_the_checksum_still_fails(self):
        # The old checksum is public arithmetic; recomputing it is exactly what
        # a forger does, so it must not rescue a tampered frame.
        mgr, rx, _ = self._pair()
        forged = bytearray(mgr.tick())
        forged[8] = int(SwarmState.ESTOP)
        utc, _, seq = struct.unpack_from("<QBI", forged, 0)
        struct.pack_into("<H", forged, 13, sync_checksum(utc, int(SwarmState.ESTOP), seq))

        assert rx.receive(bytes(forged))["status"] is FrameStatus.BAD_TAG

    def test_every_single_bit_flip_fails_the_tag(self):
        mgr, _, _ = self._pair()
        key = mgr._key
        payload = mgr.tick()
        for i in range(PAYLOAD_SIZE):
            for bit in range(8):
                tampered = bytearray(payload)
                tampered[i] ^= 1 << bit
                assert decode_broadcast_frame(bytes(tampered), key)["status"] is FrameStatus.BAD_TAG

    def test_frame_under_another_key_fails(self):
        mgr, _, _ = self._pair()
        rx = BroadcastReceiver(generate_show_key(), clock=lambda: self.NOW)
        assert rx.receive(mgr.tick())["status"] is FrameStatus.BAD_TAG

    def test_replayed_past_frame_is_rejected_by_the_sequence_counter(self):
        mgr, rx, clock = self._pair()
        mgr.set_state(SwarmState.ESTOP)
        captured = mgr.tick()
        assert rx.receive(captured)["status"] is FrameStatus.OK
        clock[0] += 0.1
        mgr.set_state(SwarmState.RUN)
        assert rx.receive(mgr.tick())["status"] is FrameStatus.OK

        # Replayed within the 200ms skew window, so only the counter stops it.
        frame = rx.receive(captured)

        assert frame["status"] is FrameStatus.REPLAYED
        assert rx.last_frame["state"] == SwarmState.RUN

    def test_the_same_frame_twice_is_a_replay(self):
        mgr, rx, _ = self._pair()
        payload = mgr.tick()
        assert rx.receive(payload)["status"] is FrameStatus.OK
        assert rx.receive(payload)["status"] is FrameStatus.REPLAYED

    def test_old_frame_beyond_200ms_is_rejected_for_skew(self):
        mgr, rx, clock = self._pair()
        payload = mgr.tick()
        clock[0] += 0.201
        assert rx.receive(payload)["status"] is FrameStatus.CLOCK_SKEW
        assert rx.last_frame_counter is None

    def test_future_frame_beyond_200ms_is_rejected_for_skew(self):
        key = generate_show_key()
        mgr = BroadcastManager(key, clock=lambda: self.NOW + 0.201)
        rx = BroadcastReceiver(key, clock=lambda: self.NOW)
        assert rx.receive(mgr.tick())["status"] is FrameStatus.CLOCK_SKEW

    def test_skew_of_exactly_200ms_is_accepted(self):
        key = generate_show_key()
        mgr = BroadcastManager(key, clock=lambda: self.NOW)
        rx = BroadcastReceiver(key, clock=lambda: self.NOW + 0.2)
        assert rx.receive(mgr.tick())["status"] is FrameStatus.OK

    def test_rejected_frames_never_advance_the_counter(self):
        # If a forged frame could raise last_frame_counter, one bogus frame
        # with counter 2^64-1 would make the fleet drop every genuine frame.
        mgr, rx, clock = self._pair()
        assert rx.receive(mgr.tick())["status"] is FrameStatus.OK
        before = rx.last_frame_counter
        forged = bytearray(mgr.tick())
        struct.pack_into("<Q", forged, 15, 2**64 - 1)
        assert rx.receive(bytes(forged))["status"] is FrameStatus.BAD_TAG
        assert rx.last_frame_counter == before
        clock[0] += 0.1
        assert rx.receive(mgr.tick())["status"] is FrameStatus.OK

    def test_counter_is_strictly_increasing_even_if_the_clock_stalls_or_steps_back(self):
        mgr, _, clock = self._pair()
        counters = []
        for step in (0.0, 0.0, -5.0, 0.0):
            clock[0] += step
            counters.append(decode_broadcast_frame(mgr.tick(), mgr._key)["frame_counter"])
        assert all(b > a for a, b in zip(counters, counters[1:]))

    def test_a_restarted_gcs_is_not_mistaken_for_a_replay(self):
        # A new BroadcastManager (GCS process restart) must not start from 0
        # and be dropped by every drone that heard the previous run.
        key = generate_show_key()
        clock = [self.NOW]
        rx = BroadcastReceiver(key, clock=lambda: clock[0])
        first = BroadcastManager(key, clock=lambda: clock[0])
        for _ in range(50):
            assert rx.receive(first.tick())["status"] is FrameStatus.OK
            clock[0] += 1 / DOWNLINK_HZ

        clock[0] += 3.0  # GCS down for a few seconds
        restarted = BroadcastManager(key, clock=lambda: clock[0])

        assert rx.receive(restarted.tick())["status"] is FrameStatus.OK

    def test_malformed_payload_is_dropped_not_raised(self):
        _, rx, _ = self._pair()
        assert rx.receive(b"\x00" * 16)["status"] is FrameStatus.MALFORMED
        assert rx.last_frame is None


class TestBroadcastManager:
    def test_transmits_to_the_broadcast_sysid(self):
        sent = []
        mgr = BroadcastManager(KEY, transmit=lambda payload, sysid: sent.append((payload, sysid)))
        mgr.tick()
        assert len(sent) == 1
        payload, sysid = sent[0]
        assert sysid == BROADCAST_SYSID == 0
        assert len(payload) == PAYLOAD_SIZE

    def test_one_frame_per_tick_regardless_of_fleet_size(self):
        sent = []
        mgr = BroadcastManager(KEY, transmit=lambda payload, sysid: sent.append(payload))
        for _ in range(10):
            mgr.tick()
        assert len(sent) == 10 == mgr.frames_sent

    def test_broadcasts_the_current_state_and_sequence(self):
        mgr = BroadcastManager(KEY, clock=lambda: 1_700_000_000.0)
        mgr.set_state(SwarmState.RUN)
        mgr.set_sequence(9)
        decoded = decode_broadcast_frame(mgr.tick(), KEY)
        assert decoded["state"] == SwarmState.RUN
        assert decoded["sequence_id"] == 9
        assert decoded["valid"]

    def test_stamps_utc_microseconds_from_the_clock(self):
        mgr = BroadcastManager(KEY, clock=lambda: 1_700_000_000.5)
        decoded = decode_broadcast_frame(mgr.tick(), KEY)
        assert decoded["utc_timestamp_usec"] == 1_700_000_000_500_000

    def test_starts_idle(self):
        assert BroadcastManager(KEY).state is SwarmState.IDLE

    def test_rejects_an_unknown_state(self):
        with pytest.raises(ValueError):
            BroadcastManager(KEY).set_state(99)


class TestStatusPayloadCodec:
    def test_round_trips_every_field(self):
        payload = encode_status_payload(
            drone_id=417,
            vbat_mv=15_800,
            current_wp_idx=63,
            rtk_status=2,
            fault_flags=0b0000_0101,
            x_err_cm=-42,
            y_err_cm=17,
            z_err_cm=-3,
        )
        assert len(payload) == STATUS_PAYLOAD_SIZE == 16
        decoded = decode_status_payload(payload)
        assert decoded == {
            "drone_id": 417,
            "vbat_mv": 15_800,
            "current_wp_idx": 63,
            "rtk_status": 2,
            "fault_flags": 0b0000_0101,
            "x_err_cm": -42,
            "y_err_cm": 17,
            "z_err_cm": -3,
        }

    def test_position_errors_are_signed(self):
        # int16 centimetres: a drone drifting north-west reports negative
        # components, and unsigned packing would read them as ~65000.
        decoded = decode_status_payload(
            encode_status_payload(0, 0, 0, 0, 0, -100, -200, -300)
        )
        assert (decoded["x_err_cm"], decoded["y_err_cm"], decoded["z_err_cm"]) == (
            -100,
            -200,
            -300,
        )

    def test_drone_id_field_covers_the_whole_fleet(self):
        decoded = decode_status_payload(encode_status_payload(999, 0, 0, 0, 0, 0, 0, 0))
        assert decoded["drone_id"] == 999

    def test_size_is_fixed_so_slots_are_predictable(self):
        # Variable-length uplink frames would make slot width a guess.
        big = encode_status_payload(65535, 65535, 65535, 255, 255, 32767, 32767, 32767)
        small = encode_status_payload(0, 0, 0, 0, 0, 0, 0, 0)
        assert len(big) == len(small) == STATUS_PAYLOAD_SIZE

    def test_reserved_tail_is_zero_filled(self):
        payload = encode_status_payload(1, 2, 3, 4, 5, 6, 7, 8)
        assert payload[14:] == bytes(STATUS_PAYLOAD_SIZE - 14)

    def test_framed_uplink_frame_is_30_bytes(self):
        assert STATUS_FRAME_SIZE == STATUS_PAYLOAD_SIZE + MAC_FRAMING_BYTES == 30

    def test_rejects_a_wrong_sized_payload(self):
        with pytest.raises(ValueError):
            decode_status_payload(b"\x00" * 8)


class TestFullFleetRound:
    def test_500_drones_each_report_once_per_epoch_in_their_own_slot(self):
        # End to end: build every drone's frame, place it in its slot, and
        # confirm the round is collision-free and inside budget.
        sched = TDMAScheduler(fleet_size=FLEET)
        airtime = 0.0
        occupied = []

        for drone_id in range(FLEET):
            payload = encode_status_payload(
                drone_id=drone_id,
                vbat_mv=16_000 - drone_id,
                current_wp_idx=drone_id % 128,
                rtk_status=2,
                fault_flags=0,
                x_err_cm=drone_id - 250,
                y_err_cm=250 - drone_id,
                z_err_cm=-(drone_id % 50),
            )
            assert decode_status_payload(payload)["drone_id"] == drone_id

            slot = sched.slot_for(drone_id)
            tx = sched.transmission_time_s()
            assert slot.start_s + tx <= slot.end_s  # fits without overrunning
            occupied.append((slot.start_s, slot.start_s + tx))
            airtime += tx

        occupied.sort()
        for (_, earlier_end), (later_start, _) in zip(occupied, occupied[1:]):
            assert earlier_end <= later_start

        # Total air actually occupied by the uplink, versus the 2s epoch.
        assert airtime / EPOCH_SECONDS < 0.25
        assert channel_utilization(FLEET) < 0.30
