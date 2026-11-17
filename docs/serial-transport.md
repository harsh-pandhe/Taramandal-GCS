# Serial vehicle links (issue #67)

By default the GCS reaches every drone on `udpin://127.0.0.1:<port>` (PX4 SITL). A drone can now be reached over a **serial port** instead, which is how a real autopilot is connected. This is a transport only: it
changes no operating mode and opens no hardware gate (see "What it does not do").

## Turning it on

```
GCS_VEHICLE_ADDRESSES="0=serial:///dev/ttyACM0:921600"
```

Comma-separated `drone-index=address` entries; a drone without an entry keeps its UDP address. Accepted addresses (anything else is refused, nothing is guessed):

- `udpin://host:port` and `udpout://host:port` (port 1-65535);
- `serial:///absolute/device/path:baud`, baud one of 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600, 1500000, 2000000. The baud rate is always given: no default is assumed. A device path with `..` is refused.

**A bad value stops the GCS from starting** (with the reason), and so does an index outside the fleet or a repeated index: a flight link must never fall back to a default or guessed address. It applies to `VEHICLE_BACKEND=px4` only.

## What the GCS does when the cable is pulled

A serial link that dies under a live `mavsdk_server` is never reopened by the server, and the connection flag stays set; only the heartbeats stop. The GCS marks the drone not connected after about 2.5 s (the existing heartbeat bar). For a serial address a
watchdog then reconnects it once the heartbeat has been stale for 3 s **and** the device is there again (it was seen missing meanwhile, or the link stayed stale 10 s), using the same locked `connect()` the exited-server respawn uses. A vehicle that stays silent is retried
after 5 s, then 10 s, then every 30 s, with a warning in the log each time. Measured on PX4 SITL through a virtual device: link seen down after 2.5 s, back 4.6 to 7.6 s after the replug, and a hover completing afterwards.

## The autopilot's stream set is part of the link

The GCS does not create bandwidth. Over a slow line the autopilot must be configured to send what the GCS needs, per message (`MAV_n_*`, `mavlink stream`). With PX4 SITL's default onboard stream set (about 25 KB/s) a 115200 or 57600 line drops most of it, the MAVLink FTP mission
upload fails and the GCS correctly refuses to arm; scaling all streams down together starves `landed_state` past the GCS's 2.5 s staleness bar and the landing confirmation fails closed. A set that worked at **57600 baud** (about 1.1 KB/s offered): HEARTBEAT 2 Hz, SYS_STATUS 2, EXTENDED_SYS_STATE 2,
LOCAL_POSITION_NED 15, GLOBAL_POSITION_INT 5, ATTITUDE 5, GPS_RAW_INT 2, BATTERY_STATUS 1, HOME_POSITION 1, ESTIMATOR_STATUS 1 (position 14.8 Hz, `landed_state` 0.99 Hz). The messages that matter most: HEARTBEAT and EXTENDED_SYS_STATE at least 1 Hz (staleness bars), LOCAL_POSITION_NED at least 10 Hz.

## Trying it without hardware

`tools/serial_bridge.py` creates a virtual serial device and bridges it to a UDP MAVLink link (PX4 SITL's onboard link):

```
python tools/serial_bridge.py --link /tmp/tara-vserial0 --udp-listen 14540 --baud 921600
GCS_VEHICLE_ADDRESSES="0=serial:///tmp/tara-vserial0:921600" ./tara up --backend px4 -n 1
```

`--baud` limits the line to baud/10 bytes a second and drops what would not fit (counted); SIGUSR1 pulls the cable (the path disappears), SIGUSR2 plugs it back. It is a stand-in for the wire, **not a real UART**.

## What it does not do

- It does not open the hardware gate: `HARDWARE` mode, `hardware_flight_certified` and every command gate are unchanged, and a serial address in `SIMULATION` is exactly as gated as a UDP one.
- It has not been run against a real autopilot, USB or UART driver, or real firmware; which controller and firmware are used is a decision for the hardware gate (#135).
- It does not discover vehicles (#58, #304) or negotiate a baud rate (MAVSDK needs it given).

Evidence, rig and all runs: [docs/sitl-serial-link/](sitl-serial-link/README.md).
