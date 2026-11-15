"""The fleet registry's rules (issue #58), with fake clocks so every timing rule is exact.

No sockets here: see test_fleet_discovery.py for the listener, and for the issue's three acceptance
criteria run with real processes.
"""

import json
import os
import struct

import pytest

from tara_gcs import fleet_registry as fr
from tara_gcs.fleet_registry import FleetRegistry, State, Status

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))


class Clock:
    """A wall clock and a monotonic clock that tests move by hand."""

    def __init__(self):
        self.wall = 1_800_000_000.0
        self.mono = 1000.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


@pytest.fixture
def clock():
    return Clock()


def make(clock, **kw):
    return FleetRegistry(key=KEY, wall_clock=lambda: clock.wall, mono_clock=lambda: clock.mono, **kw)


def frame(clock, uuid, *, key=KEY, offset_s=0.0):
    return fr.encode_announcement(key, uuid, int((clock.wall + offset_s) * 1_000_000))


# ------------------------------------------------------------------ the frame

def test_a_frame_round_trips_and_is_30_bytes(clock):
    payload = frame(clock, 0xDEADBEEF12345678)
    assert len(payload) == fr.FRAME_SIZE == 30
    got = fr.decode_announcement(payload, KEY)
    assert got["status"] is Status.OK and got["hardware_uuid"] == 0xDEADBEEF12345678


def test_a_frame_under_another_key_is_a_bad_tag(clock):
    assert fr.decode_announcement(frame(clock, 7, key=OTHER_KEY), KEY)["status"] is Status.BAD_TAG


@pytest.mark.parametrize("position", range(30))
def test_flipping_any_single_bit_is_never_accepted(clock, position):
    payload = bytearray(frame(clock, 0x1122334455667788))
    payload[position] ^= 0x01
    assert fr.decode_announcement(bytes(payload), KEY)["status"] is not Status.OK


@pytest.mark.parametrize("size", [0, 1, 29, 31, 64])
def test_a_frame_of_the_wrong_size_is_malformed(clock, size):
    assert fr.decode_announcement(b"\x00" * size, KEY)["status"] is Status.MALFORMED


def test_wrong_magic_or_version_is_malformed_even_with_a_correct_tag(clock):
    for body in (struct.pack(fr._BODY_FORMAT, b"XXXX", 1, 0, 5, 1), struct.pack(fr._BODY_FORMAT, b"TANN", 9, 0, 5, 1)):
        assert fr.decode_announcement(body + fr._tag(KEY, body), KEY)["status"] is Status.MALFORMED


def test_a_broadcast_downlink_frame_does_not_pass_as_an_announcement(clock):
    """The two protocols share a key style but not a domain: a tag for one never validates the other."""
    body = struct.pack(fr._BODY_FORMAT, b"TANN", 1, 0, 5, 1)
    import hashlib
    import hmac

    other_domain = hmac.new(KEY, b"TARA-BCAST\x01" + body, hashlib.sha256).digest()[: fr.AUTH_TAG_SIZE]
    assert fr.decode_announcement(body + other_domain, KEY)["status"] is Status.BAD_TAG


@pytest.mark.parametrize("uuid", [0, -1, 2**64, True, "1", 1.5])
def test_encode_refuses_a_bad_hardware_id(uuid):
    with pytest.raises(ValueError):
        fr.encode_announcement(KEY, uuid)


def test_a_short_key_is_refused_everywhere(clock):
    with pytest.raises(ValueError):
        fr.encode_announcement(b"short", 5)
    with pytest.raises(ValueError):
        fr.decode_announcement(b"\x00" * 30, b"short")
    with pytest.raises(ValueError):
        FleetRegistry(key=b"short")


# ---------------------------------------------------------------- joining

def test_a_new_drone_gets_the_lowest_free_slot_and_is_online(clock):
    reg = make(clock)
    a = reg.observe(frame(clock, 0xAAA))
    clock.advance(0.01)
    b = reg.observe(frame(clock, 0xBBB))
    assert (a.status, a.joined, a.entry.drone_id, a.entry.state) == (Status.OK, True, 0, State.ONLINE)
    assert (b.joined, b.entry.drone_id) == (True, 1)
    assert reg.online_ids() == [0, 1]


def test_the_pad_is_the_drone_id(clock):
    entry = make(clock).observe(frame(clock, 0xAAA)).entry
    assert entry.pad_index == entry.drone_id


def test_a_repeat_announcement_does_not_change_the_slot_and_is_not_a_join(clock):
    reg = make(clock)
    reg.observe(frame(clock, 0xAAA))
    clock.advance(0.5)
    again = reg.observe(frame(clock, 0xAAA))
    assert (again.status, again.joined, again.recovered, again.entry.drone_id) == (Status.OK, False, False, 0)


def test_the_fleet_is_full_after_max_fleet_drones(clock):
    reg = make(clock, max_fleet=2)
    for uuid in (1, 2):
        assert reg.observe(frame(clock, uuid)).status is Status.OK
        clock.advance(0.01)
    third = reg.observe(frame(clock, 3))
    assert third.status is Status.FLEET_FULL and third.entry is None
    assert reg.rejected["fleet_full"] == 1 and len(reg.entries) == 2
    # a drone that already has a slot is unaffected
    assert reg.observe(frame(clock, 1)).status is Status.OK


# --------------------------------------------------------------- the watchdog

def test_a_drone_goes_stale_after_stale_after_s_and_not_before(clock):
    reg = make(clock, stale_after_s=1.5)
    reg.observe(frame(clock, 0xAAA))
    clock.advance(1.5)
    assert reg.sweep() == [] and reg.online_ids() == [0]  # exactly at the limit is still online
    clock.advance(0.01)
    out = reg.sweep()
    assert [(t.kind, t.drone_id) for t in out] == [("stale", 0)]
    assert reg.online_ids() == [] and reg.entries[0xAAA].state is State.STALE


def test_a_stale_drone_is_reported_once_not_on_every_sweep(clock):
    reg = make(clock)
    reg.observe(frame(clock, 0xAAA))
    clock.advance(5)
    assert len(reg.sweep()) == 1
    assert reg.sweep() == [] and reg.entries[0xAAA].stale_count == 1


def test_only_the_quiet_drone_goes_stale(clock):
    reg = make(clock)
    reg.observe(frame(clock, 0xAAA))
    reg.observe(frame(clock, 0xBBB))
    for _ in range(4):
        clock.advance(0.5)
        reg.observe(frame(clock, 0xBBB))  # B keeps announcing, A does not
    assert [t.drone_id for t in reg.sweep()] == [0]
    assert reg.online_ids() == [1]


def test_a_drone_that_comes_back_is_recovered_in_its_own_slot(clock):
    reg = make(clock)
    reg.observe(frame(clock, 0xAAA))
    reg.observe(frame(clock, 0xBBB))
    clock.advance(10)
    reg.sweep()
    clock.advance(0.01)
    back = reg.observe(frame(clock, 0xBBB))
    assert (back.status, back.recovered, back.joined, back.entry.drone_id, back.entry.state) == (
        Status.OK, True, False, 1, State.ONLINE)


def test_slots_stay_reserved_for_stale_drones(clock):
    """A drone that is off must not lose its pad to a newcomer."""
    reg = make(clock)
    reg.observe(frame(clock, 0xAAA))
    clock.advance(10)
    reg.sweep()
    newcomer = reg.observe(frame(clock, 0xCCC))
    assert newcomer.entry.drone_id == 1
    assert reg.observe(frame(clock, 0xAAA)).entry.drone_id == 0


def test_stepping_the_wall_clock_does_not_make_the_fleet_stale(clock):
    reg = make(clock)
    reg.observe(frame(clock, 0xAAA))
    clock.wall += 3600  # an NTP step; the monotonic clock did not move
    assert reg.sweep() == [] and reg.online_ids() == [0]


def test_nothing_is_online_until_a_verified_announcement_arrives(clock):
    reg = make(clock)
    assert reg.online_ids() == [] and reg.sweep() == []
    reg.observe(frame(clock, 1, key=OTHER_KEY))
    reg.observe(b"junk")
    assert reg.online_ids() == [] and reg.entries == {}


# ---------------------------------------------------- rejection leaves no trace

def test_a_forged_frame_changes_nothing_and_is_counted(clock):
    reg = make(clock)
    reg.observe(frame(clock, 0xAAA))
    before = reg.snapshot()["drones"]
    clock.advance(0.2)
    out = reg.observe(frame(clock, 0xAAA, key=OTHER_KEY))
    assert out.status is Status.BAD_TAG and reg.rejected["bad_tag"] == 1
    assert reg.snapshot()["drones"][0]["last_seen"] == before[0]["last_seen"]
    # and it cannot keep a dead drone looking alive
    clock.advance(2)
    reg.observe(frame(clock, 0xAAA, key=OTHER_KEY))
    assert [t.kind for t in reg.sweep()] == ["stale"]


def test_a_frame_from_a_clock_far_from_ours_is_dropped(clock):
    reg = make(clock, max_skew_s=0.5)
    assert reg.observe(frame(clock, 0xAAA, offset_s=-0.6)).status is Status.CLOCK_SKEW
    assert reg.observe(frame(clock, 0xAAA, offset_s=+0.6)).status is Status.CLOCK_SKEW
    assert reg.observe(frame(clock, 0xAAA, offset_s=0.4)).status is Status.OK
    assert reg.rejected["clock_skew"] == 2


def test_a_replayed_or_reordered_frame_is_dropped(clock):
    reg = make(clock)
    first = frame(clock, 0xAAA)
    clock.advance(0.1)
    second = frame(clock, 0xAAA)
    assert reg.observe(second).status is Status.OK
    assert reg.observe(second).status is Status.REPLAYED  # captured and sent again
    assert reg.observe(first).status is Status.REPLAYED  # older than the last accepted
    assert reg.rejected["replayed"] == 2


def test_a_replay_cannot_revive_a_stale_drone(clock):
    reg = make(clock)
    captured = frame(clock, 0xAAA)
    reg.observe(captured)
    clock.advance(3)
    reg.sweep()
    assert reg.observe(captured).status in (Status.REPLAYED, Status.CLOCK_SKEW)
    assert reg.online_ids() == []


def test_a_rebooted_drone_is_accepted_because_its_clock_kept_running(clock):
    """The announcement carries a timestamp, not a counter, so a reboot does not look like a replay."""
    reg = make(clock)
    reg.observe(frame(clock, 0xAAA))
    clock.advance(30)  # powered off, then on again, with a clock that did not stop
    reg.sweep()
    back = reg.observe(frame(clock, 0xAAA))
    assert back.status is Status.OK and back.entry.drone_id == 0


def test_garbage_in_is_counted_and_never_raises(clock):
    reg = make(clock)
    for junk in (b"", b"\x00" * 30, b"TANN" + b"\x00" * 26, os.urandom(30), "not bytes", None, 12):
        reg.observe(junk)
    assert sum(reg.rejected.values()) == 7 and reg.accepted == 0 and reg.entries == {}


# ------------------------------------------------------------------ persistence

def test_a_returning_drone_gets_its_slot_back_after_a_gcs_restart(clock, tmp_path):
    store = tmp_path / "registry.json"
    first = make(clock, store_path=store)
    first.observe(frame(clock, 0xAAA))
    first.observe(frame(clock, 0xBBB))

    clock.advance(1)
    restarted = make(clock, store_path=store)
    assert restarted.online_ids() == []  # remembered, not heard: nothing is online yet
    assert {e.drone_id for e in restarted.entries.values()} == {0, 1}
    assert all(e.state is State.STALE and e.last_seen_mono is None for e in restarted.entries.values())
    got = restarted.observe(frame(clock, 0xBBB))
    assert (got.status, got.joined, got.recovered, got.entry.drone_id) == (Status.OK, False, True, 1)
    # a brand new drone does not take a remembered slot
    assert restarted.observe(frame(clock, 0xCCC)).entry.drone_id == 2


def test_the_saved_file_holds_bindings_only_and_no_key(clock, tmp_path):
    store = tmp_path / "registry.json"
    make(clock, store_path=store).observe(frame(clock, 0xAAA))
    text = store.read_text()
    assert json.loads(text) == {"format": "tara-fleet-registry", "version": 1, "slots": {f"{0xAAA:016x}": 0}}
    assert KEY.hex() not in text
    assert [p.name for p in tmp_path.iterdir()] == ["registry.json"]  # no temp file left behind


@pytest.mark.parametrize("content", [
    "not json", "{}", '{"slots": []}', '{"slots": {"zz": 1}}', '{"slots": {"0000000000000aaa": 99999}}',
    '{"slots": {"0000000000000aaa": 0, "0000000000000bbb": 0}}', '{"slots": {"0000000000000aaa": true}}',
    '{"slots": {"0000000000000000": 0}}',
])
def test_an_unreadable_file_starts_the_registry_empty_and_is_kept_aside(clock, tmp_path, content):
    store = tmp_path / "registry.json"
    store.write_text(content)
    reg = make(clock, store_path=store)
    assert reg.entries == {}
    assert (tmp_path / "registry.json.corrupt").read_text() == content and not store.exists()
    assert reg.observe(frame(clock, 0xAAA)).entry.drone_id == 0  # and it works


def test_an_unwritable_store_does_not_stop_the_registry(clock, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    reg = make(clock, store_path=blocker / "registry.json")  # the parent is a file
    assert reg.observe(frame(clock, 0xAAA)).status is Status.OK and reg.online_ids() == [0]


def test_release_frees_the_slot(clock, tmp_path):
    store = tmp_path / "registry.json"
    reg = make(clock, store_path=store)
    reg.observe(frame(clock, 0xAAA))
    reg.observe(frame(clock, 0xBBB))
    assert reg.release(0xAAA) is True and reg.release(0xAAA) is False
    assert reg.observe(frame(clock, 0xCCC)).entry.drone_id == 0
    assert make(clock, store_path=store).entries.keys() == {0xBBB, 0xCCC}


# -------------------------------------------------------------------- snapshot

def test_the_snapshot_says_who_is_online_and_who_is_not(clock):
    reg = make(clock)
    reg.observe(frame(clock, 0xAAA))
    clock.advance(0.1)
    reg.observe(frame(clock, 0xBBB))
    clock.advance(1.45)
    reg.sweep()
    snap = reg.snapshot()
    assert (snap["online"], snap["stale"], snap["capacity"], snap["accepted"]) == (1, 1, 1000, 2)
    by_id = {d["drone_id"]: d for d in snap["drones"]}
    assert by_id[0]["state"] == "STALE" and by_id[0]["hardware_uuid"] == f"{0xAAA:016x}" and by_id[0]["stale_count"] == 1
    assert by_id[1]["state"] == "ONLINE" and by_id[1]["age_s"] == pytest.approx(1.45)
    assert json.dumps(snap)  # JSON-serialisable as is


def test_settings_are_validated():
    with pytest.raises(ValueError):
        FleetRegistry(key=KEY, max_fleet=0)
    with pytest.raises(ValueError):
        FleetRegistry(key=KEY, max_fleet=1001)
    with pytest.raises(ValueError):
        FleetRegistry(key=KEY, stale_after_s=0)
    with pytest.raises(ValueError):
        FleetRegistry(key=KEY, max_skew_s=-1)
