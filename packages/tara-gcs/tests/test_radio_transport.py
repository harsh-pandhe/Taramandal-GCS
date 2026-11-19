"""Issue #68: the serial radio transport, against pseudo-terminals.

A pty pair stands in for the wire: the transport opens the slave side by path, the test plays the radio on the master side.
This exercises the real code path (non-blocking descriptors, termios, the event loop's readers and writers). It is NOT a real
UART or a real radio: no electrical layer, no real driver, no line-speed limit. Nothing here says anything about a radio.
"""

import asyncio
import os
import random
import struct
import time

import pytest

from tara_gcs.broadcast_manager import BroadcastReceiver, FrameStatus, SwarmState
from tara_gcs.radio_transport import (
    END, ESC, MAX_BODY_BYTES, TYPE_DOWNLINK, TYPE_HEALTH, TYPE_STATUS, LinkDeframer, SerialRadioTransport,
    crc16, encode_health, encode_link_frame,
)
from tara_gcs.tdma_scheduler import STATUS_PAYLOAD_SIZE, encode_status_payload

KEY = bytes(range(32))


# ------------------------------------------------------------------ the framing


def _frames(payloads, frame_type=TYPE_STATUS):
    return b"".join(encode_link_frame(frame_type, p) for p in payloads)


def test_every_byte_value_round_trips_including_the_boundary_and_escape_bytes():
    payload = bytes(range(256))[: MAX_BODY_BYTES - 3]
    all_bytes = bytes([END, ESC, END, ESC, ESC, END]) + bytes(range(60))
    for p in (payload, all_bytes, bytes([END]), bytes([ESC]), b""):
        assert LinkDeframer().feed(encode_link_frame(TYPE_STATUS, p)) == [(TYPE_STATUS, p)]


def test_the_boundary_byte_appears_only_at_the_two_ends_of_a_frame():
    wire = encode_link_frame(TYPE_STATUS, bytes([END, ESC, END]) * 8)
    assert wire[0] == END and wire[-1] == END and END not in wire[1:-1]


@pytest.mark.parametrize("seed", range(20))
def test_random_payloads_survive_any_chunking(seed):
    rng = random.Random(seed)
    payloads = [bytes(rng.randrange(256) for _ in range(rng.randrange(0, 60))) for _ in range(40)]
    wire = _frames(payloads)
    d, got, i = LinkDeframer(), [], 0
    while i < len(wire):
        step = rng.choice([1, 2, 3, 7, 64, 500])
        got += d.feed(wire[i:i + step])
        i += step
    assert got == [(TYPE_STATUS, p) for p in payloads]
    assert d.crc_errors == 0 and d.framing_errors == 0


def test_a_flipped_bit_anywhere_in_a_frame_is_rejected_never_decoded_wrong():
    wire = encode_link_frame(TYPE_STATUS, bytes(range(16)))
    for index in range(1, len(wire) - 1):
        for bit in range(8):
            bad = bytearray(wire)
            bad[index] ^= 1 << bit
            d = LinkDeframer()
            got = d.feed(bytes(bad))
            assert got == [] or got == [(TYPE_STATUS, bytes(range(16)))], (index, bit)
    d = LinkDeframer()
    corrupt = bytearray(wire)
    corrupt[5] ^= 0x01
    assert d.feed(bytes(corrupt)) == [] and (d.crc_errors + d.framing_errors) == 1


def test_it_resynchronises_after_garbage_and_after_a_lost_boundary():
    good = encode_link_frame(TYPE_STATUS, bytes(range(16)))
    d = LinkDeframer()
    noise = bytes([1, 2, 3, ESC, 9, END, 7, 7, 7])                   # junk, then a bad escape, then a short frame
    assert d.feed(noise + good + good) == [(TYPE_STATUS, bytes(range(16)))] * 2
    # A frame whose closing boundary was lost runs into the next one: the first is lost, the next still decodes.
    d = LinkDeframer()
    # A frame that lost its closing boundary is closed by the next frame's opening one: both still decode, in order.
    assert d.feed(good[:-1] + good) == [(TYPE_STATUS, bytes(range(16)))] * 2
    assert d.feed(good) == [(TYPE_STATUS, bytes(range(16)))]


def test_a_runaway_frame_is_bounded_and_the_next_good_frame_still_decodes():
    d = LinkDeframer()
    junk = bytes([0x41]) * 10_000                                      # no boundary for 10 kB
    assert d.feed(junk) == [] and d.framing_errors >= 1
    assert len(d._body) <= MAX_BODY_BYTES
    assert d.feed(bytes([END]) + encode_link_frame(TYPE_STATUS, b"ok")) == [(TYPE_STATUS, b"ok")]


def test_empty_frames_between_boundaries_are_ignored_not_errors():
    d = LinkDeframer()
    wire = bytes([END, END, END]) + encode_link_frame(TYPE_STATUS, b"a") + bytes([END, END])
    assert d.feed(wire) == [(TYPE_STATUS, b"a")] and d.framing_errors == 0


def test_an_oversized_payload_is_refused_at_encode_time():
    with pytest.raises(ValueError):
        encode_link_frame(TYPE_STATUS, bytes(MAX_BODY_BYTES))


def test_crc16_matches_the_published_ccitt_false_check_value():
    assert crc16(b"123456789") == 0x29B1


# ------------------------------------------------------------------ a pty "radio"


class Radio:
    """The far end of the wire: reads what the GCS sends, writes what a modem would."""

    def __init__(self, link_path):
        self.master, slave = os.openpty()
        self.slave_name = os.ttyname(slave)
        self._slave = slave                      # held open so the master never sees a hang-up while the test runs
        self.link = link_path
        self.relink()
        os.set_blocking(self.master, False)
        self.deframer = LinkDeframer()
        self.received: list[tuple[float, int, bytes]] = []

    def relink(self):
        if os.path.lexists(self.link):
            os.remove(self.link)
        os.symlink(self.slave_name, self.link)

    def pump(self):
        try:
            data = os.read(self.master, 65536)
        except (BlockingIOError, OSError):
            return
        now = time.monotonic()
        for t, p in self.deframer.feed(data):
            self.received.append((now, t, p))

    def downlinks(self):
        return [p for _, t, p in self.received if t == TYPE_DOWNLINK]

    def send(self, data: bytes):
        os.write(self.master, data)

    def close(self):
        for fd in (self.master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.fixture
def radio(tmp_path):
    r = Radio(str(tmp_path / "ttyRADIO"))
    yield r
    r.close()


async def _until(predicate, timeout=5.0, step=0.01, radio=None):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError("condition not reached in time")
        if radio is not None:
            radio.pump()
        await asyncio.sleep(step)


def _open_fds():
    return len(os.listdir("/proc/self/fd"))


# ------------------------------------------------------------------ downlink


@pytest.mark.asyncio
async def test_broadcast_frames_arrive_authenticated_with_rising_counters(radio):
    t = SerialRadioTransport(radio.link, 57600, show_key=KEY)
    t.broadcast.set_state(SwarmState.RUN)
    t.broadcast.set_sequence(42)
    await t.start()
    try:
        await _until(lambda: len(radio.downlinks()) >= 8, radio=radio)
    finally:
        await t.stop()
    receiver = BroadcastReceiver(KEY, max_skew_s=60.0)   # frames are decoded after the fact; the skew rule has its own tests
    frames = [receiver.receive(p) for p in radio.downlinks()]
    assert all(f["status"] is FrameStatus.OK for f in frames)
    assert all(f["state"] == SwarmState.RUN and f["sequence_id"] == 42 for f in frames)
    counters = [f["frame_counter"] for f in frames]
    assert counters == sorted(set(counters))
    # And a forger without the key is refused by the same receiver:
    forged = BroadcastReceiver(bytes(32), max_skew_s=60.0).receive(radio.downlinks()[0])
    assert forged["status"] is FrameStatus.BAD_TAG


@pytest.mark.asyncio
async def test_real_time_cadence_is_10hz_with_no_loss_and_bounded_jitter(radio):
    t = SerialRadioTransport(radio.link, 57600, show_key=KEY)
    await t.start()
    try:
        await _until(lambda: t.connected, radio=radio)
        await asyncio.sleep(0.05)
        await _until(lambda: t.stats["tx_frames"] >= 31, timeout=6.0, radio=radio)
    finally:
        await t.stop()
    radio.pump()
    arrivals = [a for a, tp, _ in radio.received if tp == TYPE_DOWNLINK]
    gaps = [b - a for a, b in zip(arrivals, arrivals[1:])]
    mean = sum(gaps) / len(gaps)
    assert mean == pytest.approx(0.1, abs=0.004), mean               # no drift: absolute deadlines
    assert max(gaps) < 0.25 and min(gaps) > 0.0                     # loose: a shared CI runner can stall briefly
    assert t.stats["tx_overruns"] == 0 and t.stats["late_ticks"] <= 1


@pytest.mark.asyncio
async def test_a_stalled_loop_sends_one_late_frame_and_realigns_instead_of_bursting(radio):
    """Deterministic: a fake clock stalls 0.55 s once. The ticks it missed are counted, none is sent as a burst."""
    now = [1000.0]

    def clock():
        return now[0]

    real_sleep = asyncio.sleep
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        now[0] += delay
        await real_sleep(0)

    t = SerialRadioTransport(radio.link, 57600, show_key=KEY, clock=clock)
    ticks = []
    t.broadcast.tick = lambda: ticks.append(now[0])                  # count ticks, no wire needed for this test
    t._running = True
    import tara_gcs.radio_transport as rt
    orig = rt.asyncio.sleep
    rt.asyncio.sleep = fake_sleep
    try:
        task = asyncio.create_task(t._broadcast_loop())
        for _ in range(5):
            await real_sleep(0)
        now[0] += 0.55                                               # the stall: nothing ran for 0.55 s
        for _ in range(20):
            await real_sleep(0)
        t._running = False
        await asyncio.wait_for(task, 1)
    finally:
        rt.asyncio.sleep = orig
    assert t.stats["late_ticks"] == 1 and t.stats["missed_ticks"] >= 4
    gaps = [round(b - a, 3) for a, b in zip(ticks, ticks[1:])]
    assert min(gaps) >= 0.099, gaps                                   # never two ticks close together, before or after the stall


@pytest.mark.asyncio
async def test_a_wire_that_cannot_keep_up_drops_frames_and_never_blocks_the_loop(radio):
    """No reader on the far end and a 1 kHz cadence: the pty buffer fills. Frames are dropped and counted, at most one is
    ever pending, and the event loop keeps running (a heartbeat task is never late)."""
    t = SerialRadioTransport(radio.link, 57600, show_key=KEY, hz=1000)
    await t.start()
    lags, stop = [], False

    async def heartbeat():
        last = time.monotonic()
        while not stop:
            await asyncio.sleep(0.01)
            lags.append(time.monotonic() - last - 0.01)
            last = time.monotonic()

    hb = asyncio.create_task(heartbeat())
    try:
        await _until(lambda: t.stats["tx_overruns"] > 20, timeout=15.0)   # the radio never reads
        assert len(t._pending) <= 64                                       # one frame at most (a 32-byte payload, stuffed)
        assert t.link_health()["downlink"]["serial_buffer_utilization"] is not None
    finally:
        stop = True
        await hb
        await t.stop()
    assert max(lags) < 0.25, max(lags)


# ------------------------------------------------------------------ uplink


@pytest.mark.asyncio
async def test_status_uplink_reaches_the_callback_and_the_state_store_within_the_parse_budget(radio):
    got = []
    t = SerialRadioTransport(radio.link, 57600, show_key=KEY, on_status=got.append)
    await t.start()
    try:
        await _until(lambda: t.connected, radio=radio)
        statuses = [encode_status_payload(d, 15000 + d, 100 + d, 3, 0, d, -d, 2 * d) for d in range(50)]
        radio.send(_frames(statuses))
        await _until(lambda: len(got) == 50, radio=radio)
    finally:
        await t.stop()
    assert [g["drone_id"] for g in got] == list(range(50))
    assert got[7]["vbat_mv"] == 15007 and got[7]["z_err_cm"] == 14 and got[7]["y_err_cm"] == -7
    assert sorted(t.latest_status) == list(range(50))
    h = t.link_health()["uplink"]
    assert h["frames_received"] == 50 and h["drones_reporting"] == 50
    assert h["parse_mean_ms"] < 10.0 and h["parse_max_ms"] < 10.0, h      # the issue's < 10 ms per frame


@pytest.mark.asyncio
async def test_a_noisy_line_loses_only_the_damaged_frames_and_counts_them(radio):
    got = []
    t = SerialRadioTransport(radio.link, 57600, show_key=KEY, on_status=got.append)
    await t.start()
    try:
        await _until(lambda: t.connected, radio=radio)
        wire = bytearray(_frames([encode_status_payload(d, 15000, 0, 3, 0, 0, 0, 0) for d in range(20)]))
        wire[60] ^= 0xFF                                                  # damage one frame
        radio.send(bytes([0x13, 0x37]) + bytes(wire))                     # and some leading garbage
        await _until(lambda: len(got) >= 18, radio=radio)
        await asyncio.sleep(0.1)
    finally:
        await t.stop()
    ids = [g["drone_id"] for g in got]
    assert 17 <= len(ids) <= 19 and ids == sorted(ids)
    h = t.link_health()["uplink"]
    assert h["crc_errors"] + h["framing_errors"] >= 1


@pytest.mark.asyncio
async def test_wrong_sized_status_and_unknown_types_are_counted_and_ignored(radio):
    got = []
    t = SerialRadioTransport(radio.link, 57600, show_key=KEY, on_status=got.append)
    await t.start()
    try:
        await _until(lambda: t.connected, radio=radio)
        radio.send(encode_link_frame(TYPE_STATUS, b"short") + encode_link_frame(99, b"?")
                   + encode_link_frame(TYPE_HEALTH, b"bad")
                   + encode_link_frame(TYPE_STATUS, encode_status_payload(1, 1, 1, 1, 1, 1, 1, 1)))
        await _until(lambda: len(got) == 1, radio=radio)
    finally:
        await t.stop()
    h = t.stats
    assert h["rx_bad_payload"] == 2 and h["rx_unknown_type"] == 1 and len(got) == 1


@pytest.mark.asyncio
async def test_a_failing_callback_does_not_stop_the_reader(radio):
    calls = []

    def boom(status):
        calls.append(status["drone_id"])
        raise RuntimeError("consumer bug")

    t = SerialRadioTransport(radio.link, 57600, show_key=KEY, on_status=boom)
    await t.start()
    try:
        await _until(lambda: t.connected, radio=radio)
        radio.send(_frames([encode_status_payload(d, 1, 1, 1, 1, 1, 1, 1) for d in range(5)]))
        await _until(lambda: len(calls) == 5, radio=radio)
    finally:
        await t.stop()
    assert t.stats["status_callback_errors"] == 5 and t.connected is False


@pytest.mark.asyncio
async def test_link_health_reports_the_radio_rssi_snr_and_loss(radio):
    t = SerialRadioTransport(radio.link, 57600, show_key=KEY)
    await t.start()
    try:
        await _until(lambda: t.connected, radio=radio)
        assert t.link_health()["radio"] is None                          # nothing claimed before the radio reports
        radio.send(encode_link_frame(TYPE_HEALTH, encode_health(-87, 11, 950, 50, 35)))
        await _until(lambda: t.link_health()["radio"] is not None, radio=radio)
        r = t.link_health()["radio"]
    finally:
        await t.stop()
    assert r["rssi_dbm"] == -87 and r["snr_db"] == 11 and r["packet_loss_pct"] == 5.0 and r["radio_tx_buffer_pct"] == 35
    assert 0.0 <= r["age_s"] < 5.0


# ------------------------------------------------------------------ errors and recovery


@pytest.mark.asyncio
async def test_a_missing_device_keeps_retrying_and_says_why_then_connects_when_it_appears(tmp_path):
    link = str(tmp_path / "ttyLATE")
    t = SerialRadioTransport(link, 57600, show_key=KEY, reconnect_backoff_s=(0.05,))
    await t.start()
    try:
        await _until(lambda: t.stats["connect_failures"] >= 2)
        assert not t.connected and "open failed" in t.link_health()["last_error"]
        radio = Radio(link)
        try:
            await _until(lambda: t.connected, radio=radio)
            await _until(lambda: len(radio.downlinks()) >= 3, radio=radio)
        finally:
            radio.close()
    finally:
        await t.stop()


@pytest.mark.asyncio
async def test_a_cable_pull_takes_the_link_down_and_a_replug_brings_it_back(radio, tmp_path):
    t = SerialRadioTransport(radio.link, 57600, show_key=KEY, reconnect_backoff_s=(0.05,))
    await t.start()
    try:
        await _until(lambda: len(radio.downlinks()) >= 3, radio=radio)
        sent_before = t.stats["tx_frames"]
        radio.close()                                                     # the cable is pulled: the far end vanishes
        os.remove(radio.link)
        await _until(lambda: not t.connected, timeout=5.0)
        assert t.link_health()["last_error"]
        frames_while_down = t.stats["tx_frames"]
        await asyncio.sleep(0.4)
        assert t.stats["tx_frames"] == frames_while_down                  # nothing is queued or sent into a dead link
        assert not t._pending
        radio2 = Radio(radio.link)                                        # plugged back in
        try:
            await _until(lambda: t.connected, timeout=5.0)
            await _until(lambda: len(radio2.downlinks()) >= 3, radio=radio2)
            assert t.stats["reconnects"] >= 1 and t.stats["tx_frames"] > sent_before
            assert t.link_health()["uptime_s"] is not None
        finally:
            radio2.close()
    finally:
        await t.stop()


@pytest.mark.asyncio
async def test_stop_closes_the_descriptor_and_leaks_nothing_and_is_idempotent(radio):
    before = _open_fds()
    t = SerialRadioTransport(radio.link, 57600, show_key=KEY)
    await t.start()
    await _until(lambda: t.connected, radio=radio)
    assert _open_fds() == before + 1
    await t.stop()
    await t.stop()
    assert _open_fds() == before and not t.connected
    assert t._tasks == []


@pytest.mark.asyncio
async def test_repeated_pulls_do_not_leak_descriptors(radio):
    t = SerialRadioTransport(radio.link, 57600, show_key=KEY, reconnect_backoff_s=(0.02,))
    await t.start()
    try:
        await _until(lambda: t.connected, radio=radio)
        before = _open_fds()
        for _ in range(5):
            radio.close()
            os.remove(radio.link)
            await _until(lambda: not t.connected)
            radio.__init__(radio.link)
            await _until(lambda: t.connected, radio=radio)
        assert _open_fds() <= before + 1                                   # the radio's own two fds were replaced, not added
    finally:
        await t.stop()


def test_bad_construction_is_refused(tmp_path):
    with pytest.raises(ValueError):
        SerialRadioTransport("x", show_key=b"short")
    with pytest.raises(ValueError):
        SerialRadioTransport("x", show_key=KEY, hz=0)
    with pytest.raises(ValueError):
        SerialRadioTransport("x", show_key=KEY, reconnect_backoff_s=())


def test_the_health_payload_layout_is_what_the_module_documents():
    assert len(encode_health(-100, -5, 1, 2, 3)) == struct.calcsize("<bbIIB") == 11
    assert STATUS_PAYLOAD_SIZE == 16


# ------------------------------------------------------------------ environment and the GCS endpoint

from tara_gcs import main as gcs_main  # noqa: E402
from tara_gcs.auth import Operator, Role  # noqa: E402
from tara_gcs.radio_transport import build_from_env  # noqa: E402

GOOD_KEY = "00" * 32


def test_no_device_means_no_radio():
    assert build_from_env({}) is None and build_from_env({"GCS_RADIO_DEVICE": "  "}) is None


def test_a_good_environment_builds_a_transport_with_that_device_baud_and_key():
    t = build_from_env({"GCS_RADIO_DEVICE": "/dev/ttyUSB0", "GCS_RADIO_BAUD": "115200", "GCS_SHOW_KEY": GOOD_KEY})
    assert (t.device, t.baud) == ("/dev/ttyUSB0", 115200)
    assert build_from_env({"GCS_RADIO_DEVICE": "/dev/ttyUSB0", "GCS_SHOW_KEY": GOOD_KEY}).baud == 57600


@pytest.mark.parametrize("env, message", [
    ({"GCS_RADIO_DEVICE": "ttyUSB0", "GCS_SHOW_KEY": GOOD_KEY}, "absolute path"),
    ({"GCS_RADIO_DEVICE": "/dev/../etc/x", "GCS_SHOW_KEY": GOOD_KEY}, "absolute path"),
    ({"GCS_RADIO_DEVICE": "/dev/tty USB", "GCS_SHOW_KEY": GOOD_KEY}, "absolute path"),
    ({"GCS_RADIO_DEVICE": "/dev/ttyUSB0", "GCS_RADIO_BAUD": "fast", "GCS_SHOW_KEY": GOOD_KEY}, "integer"),
    ({"GCS_RADIO_DEVICE": "/dev/ttyUSB0", "GCS_RADIO_BAUD": "12345", "GCS_SHOW_KEY": GOOD_KEY}, "not one of"),
    ({"GCS_RADIO_DEVICE": "/dev/ttyUSB0"}, "needs GCS_SHOW_KEY"),
    ({"GCS_RADIO_DEVICE": "/dev/ttyUSB0", "GCS_SHOW_KEY": "zz"}, "must be hex"),
    ({"GCS_RADIO_DEVICE": "/dev/ttyUSB0", "GCS_SHOW_KEY": "aabb"}, "at least 16 bytes"),
])
def test_a_malformed_radio_environment_is_refused_with_the_reason(env, message):
    with pytest.raises(ValueError, match=message):
        build_from_env(env)


@pytest.mark.asyncio
async def test_the_gcs_refuses_to_start_on_a_malformed_radio_setting(monkeypatch):
    monkeypatch.setenv("GCS_RADIO_DEVICE", "/dev/ttyUSB0")
    monkeypatch.setenv("GCS_SHOW_KEY", "not-hex")
    with pytest.raises(RuntimeError, match="must be hex"):
        await gcs_main.startup_event()
    assert gcs_main.radio_link is None


@pytest.mark.asyncio
async def test_the_radio_link_endpoint_says_not_configured_without_a_radio(monkeypatch):
    monkeypatch.setattr(gcs_main, "radio_link", None)
    assert await gcs_main.get_radio_link(Operator(id="v", role=Role.VIEWER)) == {"configured": False}


@pytest.mark.asyncio
async def test_the_radio_link_endpoint_reports_a_live_link_and_a_dead_one(radio, monkeypatch):
    t = SerialRadioTransport(radio.link, 57600, show_key=KEY, reconnect_backoff_s=(0.05,))
    monkeypatch.setattr(gcs_main, "radio_link", t)
    viewer = Operator(id="v", role=Role.VIEWER)
    await t.start()
    try:
        await _until(lambda: len(radio.downlinks()) >= 3, radio=radio)
        radio.send(encode_link_frame(TYPE_HEALTH, encode_health(-90, 8, 99, 1, 10)))
        await _until(lambda: t.link_health()["radio"] is not None, radio=radio)
        live = await gcs_main.get_radio_link(viewer)
        assert live["configured"] is True and live["connected"] is True
        assert live["downlink"]["frames_sent"] >= 3 and live["radio"]["rssi_dbm"] == -90
        assert live["radio"]["packet_loss_pct"] == 1.0
        radio.close()
        os.remove(radio.link)
        await _until(lambda: not t.connected)
        dead = await gcs_main.get_radio_link(viewer)
        assert dead["connected"] is False and dead["last_error"]
    finally:
        await t.stop()


@pytest.mark.real_auth
def test_the_endpoint_refuses_an_anonymous_caller(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(gcs_main, "radio_link", None)
    client = TestClient(gcs_main.app)                 # no startup events, no login
    anonymous = client.get("/api/radio-link")
    assert anonymous.status_code in (401, 403), anonymous.text
    assert "configured" not in anonymous.text
