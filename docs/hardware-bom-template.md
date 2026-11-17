# Hardware BOM, wiring and configuration: template and what the software side assumes (#134 D6, #135 section A)

Written 2026-11-17 from `main` at `db0e2b5`. **No hardware is described here.** The repository records no airframe, flight controller, battery, radio or pilot (the runbook says so in 5.3). This page does two things: it lists every value the software already assumes, so the hardware lead can check the real vehicle against them, and it gives blank tables for what only the hardware lead can supply. Every blank stays blank until a person who has the vehicle in front of them fills it in; nothing may be filled from a datasheet guess. Owner of the blanks: Harsh as owner of #135, and the hardware lead.

## 1. What the software already assumes (checked against the code)

| Item | Value in the repo | Source | What the hardware lead must confirm |
|---|---|---|---|
| Flight-controller firmware | PX4 **v1.16.2**, tested **only in simulation** (SITL + Gazebo). The firmware for real hardware is **undecided** (PX4 or ArduPilot). | `tara.toml` `[px4]` pin; `docs/diwali-runbook.md` 5.3 | Which firmware and version flies, and that the GCS-side behaviour measured on PX4 v1.16.2 SITL applies to it. It has not been tested on any other firmware. |
| Command and telemetry link | MAVLink through MAVSDK. Each vehicle is reached on a UDP address or a serial device (`GCS_VEHICLE_ADDRESSES`, for example `0=serial:///dev/ttyACM0:921600`). Serial baud must be one of 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600, 1500000, 2000000. | `packages/tara-gcs/tara_gcs/vehicle_link.py`; `docs/serial-transport.md` | The real radio or cable, its baud, and its measured throughput. **The serial path has only been exercised on a virtual serial bridge against PX4 SITL, never on a real cable or radio.** |
| Stale-link rule | A drone with no heartbeat for **2.5 s** is treated as stale; the serial watchdog starts re-linking after 3 s of that. | `drone_manager.py` `HEARTBEAT_STALE_S`, `SERIAL_RELINK_AFTER_S` | That the real link's heartbeat gaps stay well under 2.5 s in the flight area. |
| Show limits the validator enforces by default | minimum separation 1.5 m (design target 2.0 m); pad spacing 2.2 m; maximum speed 6.0 m/s; maximum acceleration 50 m/s2; yaw rate 300 deg/s; geofence radius 30 m; ceiling 120 m; soft and hard geofence margins 10 m and 15 m. | `packages/tara-core/tara_core/safety.py` | These are **software defaults, not the vehicle's limits.** The vehicle's real speed, acceleration and yaw capability must be at least what any certified show asks of it. The 35-drone Diwali show peaks at about 1.6 m/s climb and 1.3 m/s descent and holds 2.05 m minimum separation (`examples/diwali`, the show's result file). |
| Vertical rates | The show export checks pacing against PX4's default vertical limits (check name `px4_vertical_within_defaults`, in the show result files). | `examples/diwali/results/certify_100.json`, `docs/sitl-t2-recorded-acceptance/recorded-takes/show3.export-result.json` | The real vehicle's configured climb and descent limits. |
| Battery cells | **4 series cells** assumed when turning pack voltage into per-cell voltage in the preflight check. | `preflight.py` `DEFAULT_BATTERY_CELLS`, `tara.toml` `[safety]` `battery_cells` | The real pack's cell count (set `battery_cells`), chemistry, capacity and warning voltages. The repo records no capacity, mass or flight time. |
| Operational profile | `tara.toml` ships `operational_profile = "SIMULATION"`. For real hardware the file's own text says `FIELD_TEST` needs **RTK Float or Fixed** and at least **80 % charge**, and `PRODUCTION` makes every gate blocking. With no `[safety]` table the GCS falls back to `PRODUCTION`. | `tara.toml` `[safety]`, `preflight.py` | Which profile the test uses, and whether the real vehicle can meet it: a vehicle without RTK cannot pass the `FIELD_TEST` or `PRODUCTION` GNSS gate, so `ENGINEERING` (no RTK or battery checks, single-drone launch) would be the only bench profile, and that is a deliberate relaxation Harsh must approve. |
| Mission compiler target | The binary mission compiler's docstring names "STM32H7 / Pixhawk" class controllers with 1 to 2 MB of RAM as its design target. | `packages/tara-core/tara_core/compiler.py` | A design intent only. No such device is chosen or tested. |
| RC takeover | Preserved in V1; the GCS yields to an operator takeover (OFFBOARD left). | #135 text; `drone_manager.py` takeover check | That the transmitter, mode switch and kill switch exist and work (bench item). |
| Pilot, site, permissions | **Nobody named; no site; no permission recorded.** | runbook 5.3 | Harsh names the RC pilot, the site and the permissions. |

## 2. Per-vehicle BOM (blank: the hardware lead fills it)

One row per distinct part. Add a column per vehicle if parts differ.

| Part | Make and model | Serial or batch | Firmware or version | Rating (V, A, kg) | Source of this entry (datasheet, label, photo) |
|---|---|---|---|---|---|
| Airframe | | | | | |
| Flight controller | | | | | |
| Firmware | | | | | |
| GNSS / RTK | | | | | |
| Companion computer (if any) | | | | | |
| Telemetry radio or cable | | | | | |
| RC receiver and transmitter | | | | | |
| ESCs | | | | | |
| Motors and propellers (size, direction) | | | | | |
| Battery (chemistry, cells, capacity) | | | | | |
| Power module and PDB | | | | | |
| Lights or payload | | | | | |

## 3. Wiring, configuration and limits (blank)

| Item (#135 section A) | Value | Evidence attached (photo, diagram, parameter file) |
|---|---|---|
| Connector polarity, wire gauge, current and voltage ratings | | |
| Motor order and propeller direction | | |
| Take-off mass and centre of gravity | | |
| Supported flight modes and the mode switch map | | |
| Loss-of-RC and loss-of-link failsafe settings | | |
| Battery failsafe thresholds | | |
| Geofence configured in the firmware | | |
| Parameter backup (file name, checksum, date) | | |
| Known constraints or faults | | |

## 4. Bench and test record (blank; every result labelled bench, simulated or physical)

| Gate (#135 section B) | Result | Who, when | Evidence |
|---|---|---|---|
| Visual and electrical check, no exposed conductor | | | |
| Battery voltage, power distribution, ESC and motor order and direction (propellers off) | | | |
| RC link, mode switches, pilot override, disarm and kill | | | |
| GCS heartbeat, displayed mode, command acknowledgement, stale-link warning | | | |
| Exact software commit and configuration frozen before any real test | | | |

Section D (supervised physical test) needs separate approval, a named pilot and a permitted site. Do not attach a physical-flight claim without the recording or log that proves it.

## 5. What this does not do

It does not clear any vehicle to fly, it does not confirm that any software limit suits any real vehicle, and it does not replace the #135 checklist. It only makes the software-side assumptions explicit so the real parts can be checked against them.
