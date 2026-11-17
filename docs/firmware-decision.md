# Which flight-controller firmware for the first real vehicle: PX4 or ArduPilot (#135 section A)

Written 2026-11-18 by Dhiraj while Harsh is unavailable. **Status: a proposal, not a decision.** Harsh and the hardware lead own the choice, because it depends on the board that is physically on the bench, which nobody has recorded. This page puts the evidence in one place and says what to check first. It clears no vehicle to fly.

## The conflict this page exists to resolve

| What | Says |
|---|---|
| Every test, recording and acceptance run in the repo (T1, T2, serial runs, fault matrix, takeover) | **PX4 v1.16.2**, simulation only |
| The GCS's declared hardware profile (`HARDWARE_PROFILE` in `main.py`, served on `/api/mode`, see [hardware-bom-template.md](hardware-bom-template.md) section 0) | **ArduPilot** (`Pixhawk FMUv2 (ArduPilot Quad-X)`, `ArduCopter 3.6.8 / 4.x`), with no evidence behind it and a bench-audit file that was never committed |
| The runbook (5.3 and the status paragraph) | the single-drone hover path uses MAVSDK **offboard**, which it describes as PX4-only; firmware undecided |

So the only firmware that has ever been exercised is not the one the profile declares. Nothing about ArduPilot has been run.

## What the software depends on today

Counted as lines mentioning the word, case-insensitive, in non-test source on `main` (`git grep -i`, 2026-11-18): `offboard` on 81 lines of `drone_manager.py` (arming, the operator-takeover check that leaves OFFBOARD, the landing and RTL state machine) and 28 of the simulator's `fleet_state.py`; `px4` on 46 lines of `drone_manager.py`, 40 of `sim_manager.py`, 29 of the `tara doctor` checks and 14 of the service launcher; `tara.toml` pins PX4 `v1.16.2`; the serial-link runs set PX4's per-port streams with PX4's own console commands. ArduPilot's counterpart to OFFBOARD, `GUIDED`, appears nowhere in the source, and the word ArduPilot appears once, on the declared-profile line. The PX4 path has real evidence behind it: the T2 acceptance, the serial runs, 18 fault-matrix runs and the takeover tests.

## Options

| | PX4 | ArduPilot |
|---|---|---|
| Evidence today | all of it | none |
| Work to fly the first single-drone hover | check the board runs PX4 v1.16.2; bench gates (#135 B) | port the GCS mode handling, takeover detection, landing and RTL state machine, stream and failsafe configuration, and then re-run the whole PX4 acceptance (SITL T2, takeover and fault matrix) on ArduPilot SITL before any hardware step |
| Main risk | the declared board may not be supported by PX4 v1.16.2 (not checked: older FMUv2 Pixhawk boards are often flash-limited, and I have not verified which PX4 targets exist for it) | a long port with no existing acceptance, and the runbook's claim that MAVSDK offboard is PX4-only (not verified by me for ArduPilot) would have to be tested first |
| Fits the "RC takeover preserved" rule (#135) | tested: the GCS yields when the mode leaves OFFBOARD | untested |

## Proposed decision (for Harsh and the hardware lead to accept, change or reject)

1. **Fly PX4, on a board that PX4 v1.16.2 supports.** It is the only firmware with evidence, and #135 forbids an unproven control path ("record actual measurements rather than inventing a pass").
2. **Check first, before anything else:** what the real flight controller is, and whether `px4_fmu-*` for it exists in the pinned PX4 tag. If it does not, the choice is a different board running PX4 (cheapest) or a deliberate ArduPilot port (expensive, with its own SITL acceptance first).
3. **Until then, treat `HARDWARE_PROFILE` as unverified and do not quote it** as the vehicle's configuration anywhere (demo material, evidence pack, slides). Either the hardware lead confirms each line and commits the audit report, or the profile is replaced by what is true.
4. **What would change this proposal:** the bench vehicle is an ArduPilot-only board that cannot be swapped; or Harsh wants ArduPilot for a reason this page does not know (for example an existing validated airframe configuration).

## Not decided here

The board, the airframe, the pilot, the site and the permissions. Those need a person with the vehicle; the BOM template has the blanks.
