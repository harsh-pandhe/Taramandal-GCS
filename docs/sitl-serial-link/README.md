# T2: the serial vehicle link, exercised on PX4 SITL through a virtual serial device (#67)

What this is: the GCS can now be pointed at a vehicle over `serial:///device:baud` (`GCS_VEHICLE_ADDRESSES`, see [docs/serial-transport.md](../serial-transport.md)), and this folder is the evidence from flying PX4 SITL + Gazebo
through it. **There is no external autopilot on this bench.** The serial device here is a pseudo-terminal created by `tools/serial_bridge.py` and bridged to PX4 SITL's UDP link: it is the real
MAVSDK serial code path (`mavsdk_server` opening a tty, MAVLink v2 framing, MAVLink FTP, reconnect) over a **stand-in for the wire, not a real UART**. No electrical layer, no USB driver, no real autopilot
firmware, no radio. Simulation only; no hardware claim. #67 stays open for the hardware acceptance.

Rig: PX4 v1.16.2 (the local user-space build) + Gazebo Sim 8.10, headless, one drone, `tara up --backend px4 -n 1`, code at the branch of the PR that adds this folder. Each run is a fresh stack. The driver is `rig/serial_drive.py`
(a signed 1-drone Studio show flown through the repo's `tools/sitl_planned_vs_actual.py`, then a hover, then an RTL in the middle of a hover, then a cable pull and replug); `rig/serial_probe_sitecustomize.py` times every MAVSDK flight command
inside the GCS process and timestamps every position and landed-state item. Cadences are the GCS's own MAVSDK connection's, measured over the show.

## Results

| Run | Link | PX4 streams | Mission upload (FTP) | Show (planned 36 s): playback, max tracking | Hover | RTL mid-hover: HTTP, disarmed after | Position Hz (show), worst gap | landed_state Hz, worst gap | Cable pull: seen down, back after replug, next hover |
|---|---|---|---|---|---|---|---|---|---|
| `udp-2` | UDP (baseline) | - | verified | SHOW_COMPLETE: 36.01 s, 0.264 m | COMPLETED | 34.6 ms, 6.1 s | 29.63 Hz, 0.037 s | 1.0 Hz, 1.02 s | n/a |
| `ser-921600` | serial, 921600 baud | default (unlimited) | verified | SHOW_COMPLETE: 36.1 s, 0.201 m | COMPLETED | 31.4 ms, 6.4 s | 29.65 Hz, 0.04 s | 1.0 Hz, 1.018 s | 2.5 s, 5.1 s, COMPLETED |
| `ser-115200` | serial, 115200 baud | default (unlimited) | **FAILED** | no flight: the GCS refused to arm (launch gate closed, "mission FAILED") | did not complete (HTTP 409) | n/a (no flight to interrupt) | 19.45 Hz, 0.101 s | - | 2.3 s, 75.5 s, failed (HTTP 409) |
| `ser-57600` | serial, 57600 baud | default (unlimited) | **FAILED** | no flight: the GCS refused to arm (launch gate closed, "mission FAILED") | did not complete (HTTP 409) | n/a (no flight to interrupt) | 7.26 Hz, 0.577 s | - | 1.3 s, 18.8 s, failed (HTTP 409) |
| `ser-115200-r8000` | serial, 115200 baud | all streams scaled to 8000 B/s | verified | SHOW_COMPLETE: 36.1 s, 0.241 m | did not complete (HTTP 500) | n/a (no flight to interrupt) | 8.86 Hz, 0.116 s | 0.3 Hz, 3.396 s | 2.5 s, 7.6 s, COMPLETED |
| `ser-57600-r4000` | serial, 57600 baud | all streams scaled to 4000 B/s | verified | SHOW_COMPLETE: 36.08 s, 0.223 m | COMPLETED | n/a (no flight to interrupt) | 4.41 Hz, 0.233 s | 0.15 Hz, 6.827 s | 2.5 s, 7.6 s, failed (HTTP 500) |
| `ser-57600-set` | serial, 57600 baud | explicit set, position 10 Hz | verified | SHOW_COMPLETE: 36.02 s, 0.25 m | COMPLETED | 37.4 ms, not seen disarmed in 120 s | 9.89 Hz, 0.105 s | 0.99 Hz, 1.016 s | 2.5 s, 4.6 s, failed (HTTP 409) |
| `ser-57600-set2` | serial, 57600 baud | explicit set, position 10 Hz | verified | SHOW_COMPLETE: 36.02 s, 0.292 m | COMPLETED | 34.5 ms, 9.0 s | 9.89 Hz, 0.106 s | 0.99 Hz, 1.019 s | 2.5 s, 5.1 s, COMPLETED |
| `ser-57600-set3` | serial, 57600 baud | explicit set, position 15 Hz | verified | SHOW_COMPLETE: 36.0 s, 0.247 m | COMPLETED | 37.1 ms, 9.0 s | 14.83 Hz, 0.072 s | 0.99 Hz, 1.018 s | 2.5 s, 5.3 s, COMPLETED |

Command latency, measured at the MAVSDK call inside the GCS (request to the autopilot's acknowledgement), p50 / max:

| Run | arm p50 / max | offboard.start | offboard.stop | land | return_to_launch |
|---|---|---|---|---|---|
| `udp-2` | 26.2 / 28.0 ms (n=3) | 14.7 / 14.8 ms (n=3) | 13.7 / 15.5 ms (n=3) | 22.1 / 28.5 ms (n=2) | 8.2 / 8.2 ms (n=1) |
| `ser-921600` | 17.0 / 31.9 ms (n=4) | 14.9 / 16.3 ms (n=4) | 12.3 / 16.8 ms (n=4) | 19.4 / 20.0 ms (n=3) | 12.0 / 12.0 ms (n=1) |
| `ser-115200` | - | - | 1532.4 / 1532.4 ms (n=1) | - | 8.1 / 8.1 ms (n=1) |
| `ser-57600` | - | - | 13.2 / 13.2 ms (n=1) | - | 8.1 / 8.1 ms (n=1) |
| `ser-115200-r8000` | 26.6 / 30.9 ms (n=3) | 13.9 / 20.6 ms (n=3) | 14.7 / 19.6 ms (n=5) | 20.0 / 20.6 ms (n=4) | 8.1 / 8.1 ms (n=1) |
| `ser-57600-r4000` | 24.7 / 29.7 ms (n=3) | 14.3 / 18.3 ms (n=3) | 16.9 / 17.7 ms (n=5) | 24.0 / 31.6 ms (n=4) | 4.0 / 4.0 ms (n=1) |
| `ser-57600-set` | 21.3 / 23.9 ms (n=3) | 11.8 / 13.0 ms (n=3) | 15.6 / 23.2 ms (n=3) | 24.3 / 24.3 ms (n=2) | 3.8 / 3.8 ms (n=1) |
| `ser-57600-set2` | 21.7 / 27.7 ms (n=4) | 10.1 / 10.5 ms (n=4) | 19.2 / 19.9 ms (n=4) | 28.1 / 31.6 ms (n=3) | 8.3 / 8.3 ms (n=1) |
| `ser-57600-set3` | 19.3 / 24.5 ms (n=4) | 14.3 / 20.3 ms (n=4) | 15.1 / 19.2 ms (n=4) | 28.1 / 32.2 ms (n=3) | 11.6 / 11.6 ms (n=1) |

## What the evidence shows

- **At 921600 baud everything works, with the same numbers as UDP:** show 36.1 s against 36 s planned (0.201 m maximum tracking error, real-time factor 0.989), the mission uploaded by **MAVLink FTP** and verified, the hover and the RTL completed, position at 29.65 Hz and `landed_state` at 0.99 Hz, and every command acknowledged in at most 32 ms (arm 17 ms, return-to-launch 12 ms).
- **A cable pull is now recovered.** The first serial runs (before the fix in this PR) saw the link go down after 2.5 s and **never came back in 120 s**: the GCS kept a live `mavsdk_server` holding a dead device, its connection flag stayed set and only the heartbeats stopped, and `_respawn_dead_server` only acts on an *exited* server. The serial link watchdog reconnects once the heartbeat has been stale 3 s and the device is back; the rows above show the link back **4.6 to 7.6 s after the replug** and a hover completing afterwards. (A first version of the watchdog gated on the raw connected flag and did nothing in the real run; its tests only ever flipped that flag. The tests now model the real failure, and a raw-flag mutant fails 5 of them.)
- **The line speed is not the GCS's to create, and a slow line must be configured.** With PX4 SITL's default onboard stream set (about 25 KB/s on average) through a 115200 or 57600 line, the bridge dropped 56 % and 78 % of the autopilot's bytes, **the MAVLink FTP mission upload failed, and the GCS refused to arm** (launch gate closed, "mission FAILED"): fail-closed, as designed. A single `offboard.stop` took 1,532 ms on the saturated 115200 line, which is what a saturated line does to a "<100 ms" command. Scaling every stream down uniformly (`-r 8000` / `-r 4000`) let the show fly, but starved `landed_state` to one item every 3.4 s and 6.8 s, past the GCS's 2.5 s staleness bar, so the landing confirmation (#145) correctly failed closed and the hover did not complete. A real autopilot is configured per message (`MAV_n_*`, `mavlink stream`); with an explicit set sized for the line (**about 1.1 KB/s offered on average, from the bridge counters over a run of roughly 5 minutes, of the 5.76 KB/s at 57600**: HEARTBEAT 2 Hz, SYS_STATUS 2, EXTENDED_SYS_STATE 2, LOCAL_POSITION_NED 10 or 15, GLOBAL_POSITION_INT 5, ATTITUDE 5, GPS_RAW_INT 2, BATTERY_STATUS 1, HOME_POSITION 1, ESTIMATOR_STATUS 1) everything works at **57600**, with position at 14.83 Hz (requested 15; the same set at a requested 10 Hz measured 9.89 Hz, just under) and `landed_state` at 0.99 Hz. So the hardware gate needs a stream configuration for each port, not only a transport.
- **Command latency is far under 100 ms** wherever the line is not saturated (all maxima at most 33 ms), for arm, offboard start and stop, land and return-to-launch. The GCS takes off through offboard setpoints, so `offboard.start` is the takeoff command measured, not `action.takeoff`.

## Not shown, and one thing I could not explain

- **A real UART, USB driver or autopilot.** Real serial hardware adds a driver, electrical noise, a USB re-enumeration on replug, and the real firmware's own stream behaviour. The bridge **limits throughput by dropping what would not fit but does not delay what it admits**, so the latencies at 57600 omit the serialization time (about 3.6 ms for a 21-byte frame) and any queueing; they are a lower bound there.
- **The firmware.** PX4 SITL is not the hardware profile in the GCS (a Pixhawk FMUv2 with ArduPilot). Which controller and firmware are used is Harsh's decision; this does not touch it, and HARDWARE mode and its gates are unchanged.
- **One unexplained run.** In `ser-57600-set` the RTL was accepted (37 ms) but the fleet was not seen disarmed within 120 s, and the following hover was refused as an illegal transition from RTL. The identical configuration did not reproduce it in `ser-57600-set2` and `ser-57600-set3` (disarmed after 9.0 s both times). I did not find the cause (PX4's own behaviour or the GCS's tracking of it); it is 1 run of 3.
- The "RTL" cells marked n/a are runs where the preceding hover did not fly, so there was nothing to return. The tool's own verdict for the 1-drone show reads `FAIL: Gazebo min separation None m`: a minimum separation does not exist for one drone, and everything else in each record (end state, tracking error, real-time factor) is as shown.
- Two earlier attempts of this series are not included: one ran all three baud rates through the first run's still-living bridge (my runner did not stop it), the other used the watchdog before it gated on heartbeat freshness. Both are rig or code faults found and fixed during the work.
