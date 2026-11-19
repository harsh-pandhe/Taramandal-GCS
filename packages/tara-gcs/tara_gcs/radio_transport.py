"""The serial radio transport: the GCS end of the fleet RF link (issue #68).

``broadcast_manager`` and ``tdma_scheduler`` define what goes over the air; this module puts it on a wire to the ground
radio. It owns one serial device and does three things:

* sends the authenticated 10 Hz broadcast frame on a fixed cadence (absolute deadlines, so a slow tick is not followed by a
  burst and an error never accumulates);
* parses what the radio sends back: the slotted 16-byte drone status payloads and the radio's own link-health reports;
* counts everything an operator needs to judge the link (see ``link_health``).

It talks to a radio *modem* on a UART or USB serial port. Radio hardware, antennas and regional filings are out of scope, and
**nothing here has been run against a real radio**: it is tested against pseudo-terminals, which have no electrical layer, no
real driver and no real baud-rate limit.

Link framing (this module's own, between the GCS and the modem; the payloads inside are the codecs' own)::

    0xC0  stuffed( type  payload  crc16 )  0xC0

``type`` is one byte, ``crc16`` is CRC-16/CCITT-FALSE over ``type + payload``, little-endian. Stuffing is SLIP: a literal 0xC0
inside the body is sent as 0xDB 0xDC and a literal 0xDB as 0xDB 0xDD, so 0xC0 only ever means "frame boundary" and a receiver
that joins mid-stream or loses bytes resynchronises at the next boundary.

========  =======================  ================================================================
type      direction                payload
========  =======================  ================================================================
1 DOWNLINK  GCS to radio           the 32-byte broadcast frame (``broadcast_manager``)
2 STATUS    radio to GCS           the 16-byte slotted drone status (``tdma_scheduler``)
3 HEALTH    radio to GCS           ``<bbIIB``: rssi dBm, snr dB, packets received, packets lost, radio tx buffer percent
========  =======================  ================================================================

Never blocks: a write goes to a non-blocking descriptor and at most one frame is ever pending. If the previous frame has not
left (the wire or the modem is slower than the cadence) the new one is dropped and counted as an overrun, rather than queued,
because a queued authenticated frame is a stale one. A read or write error, end of file or a missing device takes the link down
and the transport reconnects with a back-off. Linux and macOS (``termios``); not Windows.
"""

from __future__ import annotations

import asyncio
import binascii
import contextlib
import fcntl
import logging
import os
import struct
import termios
import time
import tty
from collections.abc import Callable

from .broadcast_manager import DOWNLINK_HZ, BroadcastManager
from .tdma_scheduler import STATUS_PAYLOAD_SIZE, decode_status_payload

END = 0xC0
ESC = 0xDB
ESC_END = 0xDC
ESC_ESC = 0xDD

TYPE_DOWNLINK = 1
TYPE_STATUS = 2
TYPE_HEALTH = 3
_PAYLOAD_SIZES = {TYPE_STATUS: STATUS_PAYLOAD_SIZE}
_HEALTH_FORMAT = "<bbIIB"
HEALTH_PAYLOAD_SIZE = struct.calcsize(_HEALTH_FORMAT)

#: The longest body (type + payload + crc) a receiver accepts. A longer one is noise: it is dropped, and what follows is
#: resynchronised, so a missing boundary cannot make the receiver buffer without limit.
MAX_BODY_BYTES = 80

#: The kernel's pseudo-terminal output buffer, used only to turn "bytes waiting" into a rough percentage.
_TTY_OUTPUT_BUFFER_BYTES = 4096

DEFAULT_RECONNECT_BACKOFF_S = (1.0, 2.0, 5.0)


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE."""
    return binascii.crc_hqx(data, 0xFFFF)


def stuff(body: bytes) -> bytes:
    out = bytearray()
    for b in body:
        if b == END:
            out += bytes((ESC, ESC_END))
        elif b == ESC:
            out += bytes((ESC, ESC_ESC))
        else:
            out.append(b)
    return bytes(out)


def encode_link_frame(frame_type: int, payload: bytes) -> bytes:
    """One frame as it goes on the wire, boundaries included."""
    if not 0 <= frame_type <= 255:
        raise ValueError("frame type must fit a byte")
    body = bytes((frame_type,)) + bytes(payload)
    if len(body) + 2 > MAX_BODY_BYTES:
        raise ValueError(f"a frame body is at most {MAX_BODY_BYTES - 2} bytes")
    return bytes((END,)) + stuff(body + struct.pack("<H", crc16(body))) + bytes((END,))


def encode_health(rssi_dbm: int, snr_db: int, packets_received: int, packets_lost: int, tx_buffer_pct: int) -> bytes:
    """A radio-health payload (what a radio sends; used by the tests and by anyone simulating a radio)."""
    return struct.pack(_HEALTH_FORMAT, rssi_dbm, snr_db, packets_received, packets_lost, tx_buffer_pct)


class LinkDeframer:
    """Incremental decoder for the link framing. Feed it whatever bytes arrive, in any chunking."""

    def __init__(self) -> None:
        self._body = bytearray()
        self._escaped = False
        self._discarding = False
        self.crc_errors = 0
        self.framing_errors = 0  # bad escape sequence, a frame that overran MAX_BODY_BYTES, or one too short to hold a CRC
        self.frames = 0

    def reset(self) -> None:
        """Forget a half-received frame (the counters are kept)."""
        self._body.clear()
        self._escaped = False
        self._discarding = False

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """Returns the ``(type, payload)`` of every frame completed by ``data``."""
        out: list[tuple[int, bytes]] = []
        for b in data:
            if b == END:
                if self._body and not self._discarding:
                    frame = self._finish()
                    if frame is not None:
                        out.append(frame)
                self.reset()
                continue
            if self._discarding:
                continue
            if self._escaped:
                self._escaped = False
                if b == ESC_END:
                    b = END
                elif b == ESC_ESC:
                    b = ESC
                else:
                    self.framing_errors += 1
                    self._discarding = True
                    continue
            elif b == ESC:
                self._escaped = True
                continue
            self._body.append(b)
            if len(self._body) > MAX_BODY_BYTES:
                self.framing_errors += 1
                self._discarding = True
                self._body.clear()
        return out

    def _finish(self) -> tuple[int, bytes] | None:
        body = bytes(self._body)
        if len(body) < 3:  # a type byte and a CRC at the least
            self.framing_errors += 1
            return None
        data, (crc,) = body[:-2], struct.unpack("<H", body[-2:])
        if crc16(data) != crc:
            self.crc_errors += 1
            return None
        self.frames += 1
        return data[0], data[1:]


class SerialRadioTransport:
    """Owns the serial device to the ground radio. See the module docstring.

    Args:
        device: the serial device path (``/dev/ttyUSB0``). A symlink is fine.
        baud: the line speed, set where the device accepts one (a pseudo-terminal ignores it).
        show_key: the pre-shared show key the broadcast frames are authenticated with.
        on_status: called with each decoded drone status dict (``decode_status_payload`` fields plus ``received_at``). An
            exception in it is counted and logged; it never stops the reader.
        hz: broadcast cadence. 10 Hz unless a test says otherwise.
        reconnect_backoff_s: waits between reconnect attempts; the last one repeats.
        clock: monotonic seconds, injectable for tests.
    """

    def __init__(
        self,
        device: str,
        baud: int = 57600,
        *,
        show_key: bytes,
        on_status: Callable[[dict], None] | None = None,
        hz: float = DOWNLINK_HZ,
        reconnect_backoff_s: tuple[float, ...] = DEFAULT_RECONNECT_BACKOFF_S,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        if hz <= 0:
            raise ValueError("hz must be positive")
        if not reconnect_backoff_s:
            raise ValueError("reconnect_backoff_s needs at least one wait")
        self.device, self.baud = device, baud
        self.broadcast = BroadcastManager(show_key, transmit=self._submit, clock=wall_clock)
        self._on_status = on_status
        self._period = 1.0 / hz
        self._backoff = tuple(reconnect_backoff_s)
        self._clock = clock
        self._wall = wall_clock

        self._fd: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._pending = bytearray()  # the one frame that has not fully left yet
        self._deframer = LinkDeframer()
        self._connected_at: float | None = None
        self._ever_connected = False
        self._down: asyncio.Event | None = None  # created in start(), once there is a running loop

        self.latest_status: dict[int, dict] = {}
        self._radio: dict | None = None
        self.last_error: str | None = None
        self.stats = {
            "tx_frames": 0,
            "tx_bytes": 0,
            "tx_overruns": 0,
            "late_ticks": 0,
            "missed_ticks": 0,
            "rx_frames": 0,
            "rx_bytes": 0,
            "rx_unknown_type": 0,
            "rx_bad_payload": 0,
            "status_callback_errors": 0,
            "reconnects": 0,
            "connect_failures": 0,
            "parse_count": 0,
            "parse_total_s": 0.0,
            "parse_max_s": 0.0,
        }

    # ------------------------------------------------------------------ life cycle

    @property
    def connected(self) -> bool:
        return self._fd is not None

    async def start(self) -> None:
        """Start the supervisor and the broadcast loop. Returns at once; the link comes up in the background."""
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._down = asyncio.Event()
        self._tasks = [asyncio.create_task(self._supervise()), asyncio.create_task(self._broadcast_loop())]

    async def stop(self) -> None:
        """Stop both loops and close the device. Safe to call twice."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._close()

    def _open(self) -> None:
        fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            tty.setraw(fd)
            speed = getattr(termios, f"B{self.baud}", None)
            if speed is not None:
                attrs = termios.tcgetattr(fd)
                attrs[4] = attrs[5] = speed  # ispeed, ospeed
                with contextlib.suppress(termios.error):  # a pseudo-terminal has no line speed to set
                    termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        self._pending.clear()
        self._deframer.reset()
        self._connected_at = self._clock()
        self._loop.add_reader(fd, self._on_readable)

    def _close(self) -> None:
        fd, self._fd = self._fd, None
        self._connected_at = None
        self._pending.clear()
        if fd is None:
            return
        if self._loop is not None:
            with contextlib.suppress(Exception):
                self._loop.remove_reader(fd)
                self._loop.remove_writer(fd)
        with contextlib.suppress(OSError):
            os.close(fd)

    def _link_down(self, why: str) -> None:
        """Take the link down (once) and wake the supervisor to reconnect."""
        if self._fd is None:
            return
        self.last_error = why
        logging.warning("Radio link %s down: %s", self.device, why)
        self._close()
        if self._down is not None:
            self._down.set()

    async def _supervise(self) -> None:
        attempt = 0
        while self._running:
            try:
                self._open()
            except OSError as e:
                self.stats["connect_failures"] += 1
                self.last_error = f"open failed: {e.strerror or e}"
                wait = self._backoff[min(attempt, len(self._backoff) - 1)]
                attempt += 1
                await asyncio.sleep(wait)
                continue
            if self._ever_connected:
                self.stats["reconnects"] += 1
            self._ever_connected = True
            attempt = 0
            self._down.clear()
            await self._down.wait()
            # Link went down: wait the first back-off before reopening, so an unplugged device is not hammered.
            await asyncio.sleep(self._backoff[0])

    # ------------------------------------------------------------------ transmit

    async def _broadcast_loop(self) -> None:
        """Fixed cadence on absolute deadlines. A late wake sends one frame and re-anchors; it never bursts to catch up."""
        deadline = self._clock()
        while self._running:
            deadline += self._period
            await asyncio.sleep(max(0.0, deadline - self._clock()))
            now = self._clock()
            if now - deadline > self._period:
                # Woke up more than a period late (a stalled loop, or the host was busy). Send ONE frame now and
                # re-anchor on it: keeping the old grid would put the next tick right behind this one, a burst.
                self.stats["late_ticks"] += 1
                self.stats["missed_ticks"] += int((now - deadline) // self._period)
                deadline = now
            self.broadcast.tick()

    def _submit(self, payload: bytes, target_system: int) -> None:
        """BroadcastManager's transmit hook: queue one frame if the previous one has left, else count an overrun."""
        if self._fd is None:
            return  # down: nothing is sent, and nothing is queued to go out late
        if self._pending:
            self.stats["tx_overruns"] += 1
            return
        self._pending += encode_link_frame(TYPE_DOWNLINK, payload)
        self.stats["tx_frames"] += 1
        self._flush()

    def _flush(self) -> None:
        fd = self._fd
        if fd is None or not self._pending:
            return
        try:
            n = os.write(fd, self._pending)
        except BlockingIOError:
            n = 0
        except OSError as e:
            self._link_down(f"write failed: {e.strerror or e}")
            return
        self.stats["tx_bytes"] += n
        del self._pending[:n]
        if self._pending:
            self._loop.add_writer(fd, self._on_writable)
        else:
            with contextlib.suppress(Exception):
                self._loop.remove_writer(fd)

    def _on_writable(self) -> None:
        self._flush()

    # ------------------------------------------------------------------ receive

    def _on_readable(self) -> None:
        fd = self._fd
        if fd is None:
            return
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            return
        except OSError as e:
            self._link_down(f"read failed: {e.strerror or e}")
            return
        if not data:
            self._link_down("end of file (device closed)")
            return
        self.stats["rx_bytes"] += len(data)
        for frame_type, payload in self._deframer.feed(data):
            started = time.perf_counter()
            self._handle(frame_type, payload)
            took = time.perf_counter() - started
            self.stats["parse_count"] += 1
            self.stats["parse_total_s"] += took
            self.stats["parse_max_s"] = max(self.stats["parse_max_s"], took)

    def _handle(self, frame_type: int, payload: bytes) -> None:
        self.stats["rx_frames"] += 1
        now = self._wall()
        if frame_type == TYPE_STATUS:
            try:
                status = decode_status_payload(payload)
            except ValueError:
                self.stats["rx_bad_payload"] += 1
                return
            status["received_at"] = now
            self.latest_status[status["drone_id"]] = status
            if self._on_status is not None:
                try:
                    self._on_status(status)
                except Exception:
                    self.stats["status_callback_errors"] += 1
                    logging.exception("Radio status callback failed")
        elif frame_type == TYPE_HEALTH:
            if len(payload) != HEALTH_PAYLOAD_SIZE:
                self.stats["rx_bad_payload"] += 1
                return
            rssi, snr, received, lost, buf = struct.unpack(_HEALTH_FORMAT, payload)
            self._radio = {
                "rssi_dbm": rssi, "snr_db": snr, "packets_received": received, "packets_lost": lost,
                "tx_buffer_pct": buf, "received_at": now,
            }
        else:
            self.stats["rx_unknown_type"] += 1

    # ------------------------------------------------------------------ health

    def _serial_buffer_utilization(self) -> float | None:
        """Share of the device's output buffer in use, from TIOCOUTQ. Approximate: the buffer size is assumed."""
        if self._fd is None:
            return None
        try:
            queued = struct.unpack("i", fcntl.ioctl(self._fd, termios.TIOCOUTQ, struct.pack("i", 0)))[0]
        except OSError:
            return None
        return round(min(1.0, queued / _TTY_OUTPUT_BUFFER_BYTES), 3)

    def link_health(self) -> dict:
        """Everything an operator HUD needs about the radio link, as plain JSON-able values."""
        s = self.stats
        radio = None
        if self._radio is not None:
            r = self._radio
            total = r["packets_received"] + r["packets_lost"]
            radio = {
                "rssi_dbm": r["rssi_dbm"],
                "snr_db": r["snr_db"],
                "packet_loss_pct": round(100.0 * r["packets_lost"] / total, 2) if total else 0.0,
                "radio_tx_buffer_pct": r["tx_buffer_pct"],
                "age_s": round(max(0.0, self._wall() - r["received_at"]), 2),
            }
        parsed = s["parse_count"]
        return {
            "device": self.device,
            "baud": self.baud,
            "connected": self.connected,
            "uptime_s": round(self._clock() - self._connected_at, 1) if self._connected_at is not None else None,
            "last_error": self.last_error,
            "reconnects": s["reconnects"],
            "connect_failures": s["connect_failures"],
            "downlink": {
                "hz": round(1.0 / self._period, 3),
                "frames_sent": s["tx_frames"],
                "overruns": s["tx_overruns"],
                "late_ticks": s["late_ticks"],
                "missed_ticks": s["missed_ticks"],
                "serial_buffer_utilization": self._serial_buffer_utilization(),
            },
            "uplink": {
                "frames_received": s["rx_frames"],
                "crc_errors": self._deframer.crc_errors,
                "framing_errors": self._deframer.framing_errors,
                "bad_payloads": s["rx_bad_payload"],
                "unknown_types": s["rx_unknown_type"],
                "drones_reporting": len(self.latest_status),
                "parse_mean_ms": round(1000.0 * s["parse_total_s"] / parsed, 4) if parsed else None,
                "parse_max_ms": round(1000.0 * s["parse_max_s"], 4) if parsed else None,
                "callback_errors": s["status_callback_errors"],
            },
            "radio": radio,
        }


def build_from_env(env=None, **kwargs) -> SerialRadioTransport | None:
    """The transport the environment asks for, or None when ``GCS_RADIO_DEVICE`` is not set.

    ``GCS_RADIO_DEVICE`` the serial device path; ``GCS_RADIO_BAUD`` its speed (one of ``vehicle_link.BAUD_RATES``, default
    57600); ``GCS_SHOW_KEY`` the show key as hex, at least 16 bytes (required with a device). A malformed value raises
    ``ValueError``: a radio that silently fell back to "none" or to a default key would leave the operator believing the
    fleet hears authenticated frames. An unreachable device is not an error here; the transport retries and reports it.
    """
    from .broadcast_manager import SHOW_KEY_MIN_BYTES
    from .vehicle_link import BAUD_RATES

    env = os.environ if env is None else env
    device = (env.get("GCS_RADIO_DEVICE") or "").strip()
    if not device:
        return None
    if not os.path.isabs(device) or ".." in device.split("/") or any(c.isspace() for c in device):
        raise ValueError(f"GCS_RADIO_DEVICE must be an absolute path without '..' or spaces, got {device!r}")
    raw_baud = (env.get("GCS_RADIO_BAUD") or "57600").strip()
    try:
        baud = int(raw_baud)
    except ValueError:
        raise ValueError(f"GCS_RADIO_BAUD must be an integer, got {raw_baud!r}") from None
    if baud not in BAUD_RATES:
        raise ValueError(f"GCS_RADIO_BAUD {baud} is not one of {sorted(BAUD_RATES)}")
    raw_key = (env.get("GCS_SHOW_KEY") or "").strip()
    if not raw_key:
        raise ValueError("GCS_RADIO_DEVICE needs GCS_SHOW_KEY (hex, at least 16 bytes): broadcast frames are always authenticated")
    try:
        key = bytes.fromhex(raw_key)
    except ValueError:
        raise ValueError("GCS_SHOW_KEY must be hex") from None
    if len(key) < SHOW_KEY_MIN_BYTES:
        raise ValueError(f"GCS_SHOW_KEY must be at least {SHOW_KEY_MIN_BYTES} bytes ({2 * SHOW_KEY_MIN_BYTES} hex characters)")
    return SerialRadioTransport(device, baud, show_key=key, **kwargs)
