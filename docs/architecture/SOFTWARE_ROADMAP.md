# Taramandal Software Engineering Roadmap

**Scope:** Public Implementation Roadmap for Studio, GCS, Core, and Simulator  
**Status:** Engineering Execution Roadmap  

---

## 1. Roadmap Overview

This roadmap defines the engineering progression required to transition the Taramandal software platform from its current prototype state into a production-grade, airworthy software suite.

```
Phase 0: Developer Infrastructure & Tooling
   ↓
Phase 1: Mission Contract & Autonomous Execution Protocol
   ↓
Phase 2: Fleet State Machine & Operational Safety Checklist
   ↓
Phase 3: High-Fidelity Simulation & Fault Injection
   ↓
Phase 4: Observability & Blackbox Flight Replay
   ↓
Phase 5: Cryptographic Security & Access Control
   ↓
Phase 6: Hardware-in-the-Loop (HIL) Serial Integration
   ↓
Phase 7: Full-Scale CI/CD Automated Quality Gates
```

---

## 2. Phase Breakdown

### Phase 0: Developer Infrastructure & Tooling
- Dynamic virtualenv and autopilot environment path resolution (#46).
- Standardized port alignment across GCS (8090) and Studio (8001).
- Cross-platform Playwright visual regression baseline generation (#45).

### Phase 1: Mission Contract & Autonomous Execution Protocol
- Formal JSON Schema / Protobuf specification for show export (#55).
- Deterministic per-drone binary mission compiler (`.tarm`) (#56).
- MAVLink Mission Protocol pre-upload and synchronized broadcast trigger (#47).
- Autonomous onboard trajectory follower engine for PX4/ArduPilot (#57).

### Phase 2: Fleet State Machine & Operational Safety Checklist
- Formal fleet lifecycle finite state machine preventing illegal commands (#52).
- Centralized `PreArmChecklistEngine` with operational profiles (`SIMULATION`, `ENGINEERING`, `FIELD_TEST`, `PRODUCTION`) (#48).
- Dynamic geofence envelope calculation based on choreography bounds (#53).
- Selective individual-drone abort without whole-show termination (#59).

### Phase 3: High-Fidelity Simulation & Fault Injection
- Synthetic RF channel contention, packet drop, and TDMA slot jitter injection (#62).
- Synthetic sensor degradation: RTK ambiguity drop, battery sag, rotor thrust loss (#63).
- Aerodynamic battery discharge and power consumption modeling in Studio (#54).
- Trajectory feasibility validator for velocity, acceleration, and third-derivative jerk (#60).

### Phase 4: Observability & Flight Incident Replay
- High-rate (10Hz) structured JSONL flight incident blackbox recorder (#65).
- In-browser 3D time-scrubbing replay tool with synchronized telemetry playback (#65).

### Phase 5: Cryptographic Security & Access Control
- Ed25519 digital signatures for show trajectory files (#49).
- Authenticated HMAC-SHA256 tags and anti-replay counters for broadcast commands (#50).
- Role-based access control (RBAC), session tokens, and secure key storage on GCS (#64).

### Phase 6: Hardware-in-the-Loop (HIL) Serial Integration
- Serial radio transport interface for broadcast downlink and slotted TDMA telemetry (#68).
- Serial telemetry and mission staging adapter for external MAVLink autopilots (#67).

### Phase 7: Scale Optimization & CI/CD Automation
- Vectorized bulk telemetry ingestion eliminating per-drone asyncio task overhead at 1,000 drones (#51).
- Automated GitHub Actions quality gates: Vitest, Playwright, Ruff, Mypy, and 100-drone swarm regression (#66).
