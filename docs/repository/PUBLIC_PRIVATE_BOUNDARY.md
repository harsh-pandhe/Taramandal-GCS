# Taramandal Public / Private Engineering Boundary Specification

**Status:** ARCHITECTURAL GOVERNANCE BASELINE  
**Author:** Repository Architect / Principal Engineer & Software Release Architect  
**Scope:** Taramandal Repository Governance & Separation Policy  
**Applies To:** [harsh-pandhe/Taramandal-GCS](https://github.com/harsh-pandhe/Taramandal-GCS) & Local Engineering Workspaces  

---

## 1. Core Architectural Principle

The GitHub repository `harsh-pandhe/Taramandal-GCS` is the canonical home of the **software product**:
1. **Taramandal Studio:** 3D drone show choreography, timeline sequencing, mesh voxelization, and show compilation.
2. **Taramandal GCS:** Swarm command-and-control station, telemetry visualization, safety interlocks, and mission orchestration.
3. **Core Shared Libraries & Simulations:** Pure mathematical geometry, spatial collision detection, mission codecs, and development simulations.

The public/team software repository is **NOT** a storage dump for physical drone engineering, confidential research, proprietary avionics hardware, manufacturing IP, or investor business material.

---

## 2. The Software / Hardware Interface Boundary

A modern autonomous drone swarm platform inherently interacts with physical avionics, power distribution systems, radio transceivers, and satellite navigation hardware. However, **software architecture must cleanly decouple abstract interfaces from confidential physical implementation details**.

The software repository may define and implement **abstract software interfaces, protocols, data schemas, and hardware-in-the-loop (HIL) adapters**, but must **never** contain proprietary board layouts, RF matching schematics, tuning values, confidential vendor BOMs, or internal manufacturing recipes.

### Detailed Boundary Matrix

| Engineering Domain | Public Software Boundary (Allowed in GitHub) | Private Engineering Boundary (Forbidden in GitHub) |
| :--- | :--- | :--- |
| **Radio / Telemetry** | Abstract `RadioTransport` class, packet framing, serial port abstractions, bitwise protocol codecs, simulated packet loss. | Exact SX1262 register configurations, RF matching network schematics, PCB microstrip geometry, antenna impedance tuning, proprietary power amplifier designs, RF regulatory dossiers. |
| **GNSS / RTK** | Generic `GNSSProvider` interface, MAVLink `GPS_RAW_INT` / `RTK_BASELINE` parsers, NMEA/UBX frame parsers, simulated dilution of precision (DOP). | Proprietary multi-band active antenna LNA schematics, RF noise budgets, RF ground plane optimization, ZED-F9P pinout trade studies, supplier pricing, vendor supply chain data. |
| **Avionics & Flight Controller** | Standard MAVLink v2 protocol adapters, PX4 / ArduPilot message bridges, waypoint protocol parsers, autonomous mission state machines. | Custom STM32H7 flight controller PCB layouts, ESC 4-in-1 gate-drive loop layouts, power distribution trace width calculations, current sensing amplifier circuits, pin allocation matrices. |
| **Power & Propulsion** | Abstract aerodynamic hover power equations (momentum theory), battery state-of-charge (SoC) polynomial estimators, software low-voltage warnings. | Exact custom Li-ion pack assembly instructions, proprietary cell supplier contracts, internal battery thermal runaway test data, ESC MOSFET thermals, custom battery enclosure CAD. |
| **Airframe & Mechanics** | Point-mass collision bounding spheres, downwash cone aerodynamic penalty models ($30^\circ$ cone angle), virtual 3D bounding boxes. | Full airframe STEP/SolidWorks CAD models, carbon fiber layups, CNC routing files, mold tooling specifications, injection molding drafts, vendor drop-test footage. |
| **Safety & Regulation** | Configurable pre-arm checklist engine, dynamic geofence computation, individual-drone abort state transitions. | DGCA RPTO advisor briefings, confidential regulatory compliance filings, SORA private safety cases, internal incident investigation reports. |
| **Business & Strategy** | Open-source or team-shared developer documentation, public API documentation, software onboarding guides. | Investor pitch decks, financial valuations, unit economics models, commercial client contracts, partner NDAs, market strategy documents. |

---

## 3. Concrete Examples

### Example A: Radio Communications
- **PUBLIC-SAFE:**
  ```python
  class RadioTransport(ABC):
      """Abstract interface for bidirectional swarm communication."""
      @abstractmethod
      async def send_broadcast(self, payload: bytes) -> bool: ...
      @abstractmethod
      async def receive_packet(self) -> Optional[RadioPacket]: ...
  ```
- **PRIVATE / CONFIDENTIAL (Must be relocated):**
  - PCB schematics showing the coupling between the SX1262 LoRa module and the power amplifier.
  - S-parameter RF impedance matching curves, harmonic filter calculations, and microstrip trace clearances.

### Example B: GNSS & Positioning
- **PUBLIC-SAFE:**
  ```python
  class RTKStatus(IntEnum):
      NO_GPS = 0
      FIX_2D = 1
      FIX_3D = 2
      RTK_FLOAT = 5
      RTK_FIXED = 6
  ```
- **PRIVATE / CONFIDENTIAL (Must be relocated):**
  - `V1_GNSS_ACTIVE_ANTENNA_RF_BUDGET.csv`
  - `V1_GNSS_LNA_RF_BUDGET.csv`
  - `V1_ZED_F9P_04B_PIN_AUDIT.csv`
  - Component supplier pricing and vendor procurement links.

### Example C: Mission Execution
- **PUBLIC-SAFE:**
  - Binary mission compiler converting 4D waypoints into fixed 16-byte structs (`uint32 timestamp`, `int32 x`, `int32 y`, `int32 z`, `int16 yaw`, `uint8 r,g,b,w`).
  - MAVLink Mission Protocol state machine.
- **PRIVATE / CONFIDENTIAL (Must be relocated):**
  - Custom ESC firmware flashing scripts, gate driver tuning parameters, and board-level bring-up logs.

---

## 4. Developer Governance Rule: Staging Verification

To prevent accidental data leakage into the public/team repository, all developers and automated tools must follow this strict rule:

> **CRITICAL PRE-STAGE RULE:**  
> Before running `git add`, every developer must execute `git status --short` and verify that **no private engineering, hardware, PCB, CAD, BOM, RF, or business file** is staged.  
> Never run `git add .` or `git add -A` from the repository root when untracked files are present.

---

## 5. Enforcement Mechanism

1. **Repository Scope:** The Git tracking scope is restricted to `apps/`, `packages/`, `sim/`, `tests/`, `tools/`, `docs/`, and `.github/`.
2. **Explicit `.gitignore` Protection:** All hardware directories (`hardware/`), planning directories (`plan/`), manufacturing packs (`manufacturing_release/`), routing experiments (`routing_experiments/`), and root engineering documents (`00_*` through `23_*`, `V1_*`) must be formally excluded from Git tracking.
3. **Physical Relocation:** Confirmatory separation requires relocating private files outside the physical directory tree of the Git clone into a dedicated private engineering workspace (`/home/harsh-pandhe/Taramandal-Private`).
