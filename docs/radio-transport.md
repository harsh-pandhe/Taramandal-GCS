# Serial radio transport (#68)

`packages/tara-gcs/tara_gcs/radio_transport.py` is the GCS end of the fleet RF link: it sends the authenticated 10 Hz broadcast frame (`broadcast_manager`) to a ground-radio modem over a serial device and parses what the modem sends back (the slotted drone status payloads of `tdma_scheduler`, and the modem's own link-health reports).

**Tier: software, tested against pseudo-terminals only.** A pty has no electrical layer, no real driver and no line-speed limit. Nothing here has been run against a real radio, and nothing in it says anything about range, loss or latency over the air.

## Wire format between the GCS and the modem

`0xC0  stuffed( type  payload  crc16 )  0xC0`. SLIP stuffing (0xC0 as 0xDB 0xDC, 0xDB as 0xDB 0xDD), so a receiver that joins mid-stream or loses bytes resynchronises at the next 0xC0. `crc16` is CRC-16/CCITT-FALSE over `type + payload`, little-endian. A body is at most 80 bytes; a longer one is treated as noise.

| type | direction | payload |
|---|---|---|
| 1 | GCS to radio | the 32-byte broadcast frame |
| 2 | radio to GCS | the 16-byte status payload (`tdma_scheduler`) |
| 3 | radio to GCS | `<bbIIB`: RSSI dBm, SNR dB, packets received, packets lost, radio tx buffer % |

A modem firmware has to implement this framing; no such firmware exists in this repo. Type 3 is this module's own definition, not an existing radio's report format.

## Behaviour

* **Cadence:** absolute deadlines (no drift). A wake-up more than one period late sends one frame, counts the late tick and the ticks it missed, and re-anchors, so there is never a burst.
* **Never blocks:** a non-blocking descriptor and at most one pending frame. If the previous frame has not left, the new one is dropped and counted as an overrun, because a queued authenticated frame is a stale one.
* **Errors:** a read or write error, end of file or a missing device takes the link down, drops anything pending, and reconnects with a back-off. Nothing is queued or sent while down.
* **Uplink:** a corrupt frame (CRC or framing) is counted and dropped; a wrong-sized or unknown frame is counted and ignored; an exception in the status callback is counted and never stops the reader.

## Configuration and health

`GCS_RADIO_DEVICE` (absolute path), `GCS_RADIO_BAUD` (default 57600, one of `vehicle_link.BAUD_RATES`), `GCS_SHOW_KEY` (hex, at least 16 bytes, required). With none set there is no radio. A malformed value stops the GCS from starting; an unreachable device does not (the transport retries and says so). The operations dashboard shows a link-health strip (UP / DOWN / NO UPDATE, the modem's RSSI, SNR and loss, downlink overruns, uplink errors, drones reporting) only when a radio is configured; values the modem has not reported show a dash. `GET /api/radio-link` (viewer role) returns `{"configured": false}` or the link health: connection, uptime, last error, reconnects, downlink frames/overruns/late ticks/serial buffer use, uplink frames/CRC and framing errors/drones reporting/parse time, and the modem's RSSI, SNR, loss and buffer.

## Measured here (pty, this laptop)

The 57 tests in `test_radio_transport.py` (framing fuzz, cadence, authentication, uplink, health, overrun, cable pull and replug, descriptor leaks, environment, endpoint login) pass, also four times in a row under 32 busy loops on 16 cores, and seven browser tests cover the dashboard strip. Seven deliberate breakages (no CRC check, no unstuffing, queueing instead of dropping, a drifting cadence, queueing while down, an endpoint without login) each fail a test. Parse cost is measured per status frame and asserted under the issue's 10 ms (it is far below that on this machine; the asserted bound is loose on purpose). The 10 Hz cadence test asserts the mean period within 4 ms of 100 ms over 30 frames.

## Not done, and why

* **Real radio:** needs hardware. Open: sustained 10 Hz at the radio's real baud, real loss, and the modem firmware.
* **Fleet state is not on the air:** the broadcast carries state `IDLE` and sequence 0 unless something calls `transport.broadcast.set_state` / `set_sequence`; the GCS lifecycle is not mapped onto it yet.
* **Serial buffer use** comes from `TIOCOUTQ` against an assumed 4096-byte buffer, so it is approximate.
* **Linux and macOS only** (`termios`); no `pyserial` dependency was added.
