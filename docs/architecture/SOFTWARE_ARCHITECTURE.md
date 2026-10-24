# Taramandal Software Platform Architecture

**Scope:** Canonical Software Suite (Studio, GCS, Core, Sim)  
**Status:** Public Architectural Reference  

---

## 1. System Overview

Taramandal is an end-to-end software platform designed for choreographing, simulating, and orchestrating autonomous drone light shows. The platform consists of four core software subsystems:

```
┌────────────────────────────────────────────────────────┐
│                   Taramandal Studio                    │
│   (3D Choreography, Pacing, Voxelizer, Exporter)       │
└───────────────────────────┬────────────────────────────┘
                            │ Canonical Show Contract (.json / .tarm)
                            ▼
┌────────────────────────────────────────────────────────┐
│                   Taramandal Core                      │
│   (cKDTree Spatial Safety, Hungarian Optimization)     │
└───────────────────────────┬────────────────────────────┘
                            │ Pre-Compiled Waypoints
                            ▼
┌────────────────────────────────────────────────────────┐
│                    Taramandal GCS                      │
│    (Fleet State Machine, Safety Interlocks, MAVLink)   │
└───────────────────────────┬────────────────────────────┘
                            │ Telemetry & Synchronization
                            ▼
┌────────────────────────────────────────────────────────┐
│                   Taramandal Sim                       │
│     (Vectorized NumPy 1000-Drone Kinematic Engine)     │
└────────────────────────────────────────────────────────┘
```

---

## 2. Subsystem Responsibilities

### 2.1 Taramandal Studio (`packages/tara-studio`, `packages/tara-studio-web`)
- **Choreography Workspace:** 3D scene editing built on React 18, Three.js, and React Three Fiber.
- **Voxelization Engine:** Converts 2D vectors, text glyphs, and 3D polygon meshes into distributed drone point clouds.
- **Hungarian Cost Optimizer:** Minimizes cumulative distance traveled during formation transitions.
- **Downwash & Separation Safety:** Enforces minimum 3-meter inter-drone clearance and $30^\circ$ aerodynamic downwash cones.
- **Pacing Engine:** Auto-paces transitions based on fleet size, max velocity ($v_{\max} \le 4\text{ m/s}$), and acceleration limits.

### 2.2 Taramandal GCS (`packages/tara-gcs`, `packages/tara-web`)
- **Flight Operations Dashboard:** WebGL-rendered audience NightSkyView and Cesium geospatial EarthView.
- **Fleet State Machine:** Centralized finite state machine managing stages: `STAGED -> PREARM -> ARMED -> TAKEOFF -> RUNNING -> SHOW_COMPLETE -> LANDED`.
- **Pre-Arm Safety Interlocks:** Deterministic checklist engine validating operational profiles (`SIMULATION`, `ENGINEERING`, `FIELD_TEST`, `PRODUCTION`).
- **Dynamic Geofence Engine:** Automated convex hull / bounding box envelope calculation.

### 2.3 Taramandal Core (`packages/tara-core`)
- Pure Python/NumPy library with zero external UI or network dependencies.
- Reusable algorithms: Hungarian assignment, geometry transforms, spatial hashing, and binary mission serialization.

### 2.4 Taramandal Sim (`packages/tara-sim`)
- Pure NumPy vectorized kinematics simulator capable of updating 1,000 aircraft positions in sub-microsecond tick speeds.
- Synthetic fault injection: GPS/RTK ambiguity degradation, battery sag, and packet loss modeling.

---

## 3. Communication Protocols

- **Mission Pre-Upload:** Shows are compiled deterministically into binary waypoint payloads and uploaded to aircraft storage during staging.
- **Synchronized Broadcast Trigger:** Simultaneous show start executed via synchronized UTC epoch timestamp broadcasts.
- **Downlink Telemetry:** Slotted TDMA telemetry frame ingestion feeding real-time operations dashboards.
