# Taramandal Software Engineering Dependency Graph

**Classification:** Public-Safe Architecture Specification  
**Version:** 1.0.0  
**Scope:** Canonical Software Pipelines (Studio, GCS, Core, Sim)

---

## 1. End-to-End Mission Pipeline

The primary mission compilation, upload, and autonomous execution flow:

```mermaid
graph TD
    subgraph Studio ["Taramandal Studio (Choreography & Design)"]
        UI["Studio Web UI / Scene Sequencer"] --> Forms["Formation Generation & Voxelization"]
        Forms --> Bounds["Formation Bounding & Constraint Check"]
        Bounds --> TrajGen["Trajectory Generation & Pacing"]
        TrajGen --> StudioVal["Studio Safety Validator (tara_core.validate)"]
    end

    subgraph Contract ["Canonical Mission Contract"]
        StudioVal --> Export["Mission Exporter (JSON / .tarm)"]
        Export --> Schema["Versioned Show Schema (tara_core.show)"]
    end

    subgraph Compiler ["Mission Compiler & Staging"]
        Schema --> PreVal["GCS Upload Gate (collision_errors)"]
        PreVal --> OptAssign["Hungarian Pad-to-Target Assignment"]
        OptAssign --> Splitter["Per-Drone Trajectory Decomposer"]
        Splitter --> BinaryPacker["Deterministic Binary Mission Compiler"]
    end

    subgraph GCS ["Taramandal GCS (Fleet Management)"]
        BinaryPacker --> Staging["GCS Fleet Staging & Verification"]
        Staging --> PreArm["Deterministic Pre-Arm Safety Gate"]
        PreArm --> Transport["Authenticated Transport Layer"]
    end

    subgraph Fleet ["Drone Fleet Execution"]
        Transport --> PreUpload["Onboard Flash / Storage Pre-Upload"]
        PreUpload --> HashCheck["Mission Hash / Signature Verification"]
        HashCheck --> SyncTrigger["Synchronized UTC Start Broadcast"]
        SyncTrigger --> AutonomousExec["Autonomous Onboard Trajectory Execution"]
    end
```

---

## 2. Simulation & Design-Time Validation Pipeline

The closed-loop design and verification pipeline before flight staging:

```mermaid
graph TD
    subgraph StudioDesign ["Studio Choreography"]
        Choreo["Multi-Scene Choreography Dataset"] --> GeomSweep["Geometry & Dynamics Sweep"]
    end

    subgraph CoreValidation ["Deterministic Core Validation (tara-core)"]
        GeomSweep --> ProximityCheck["cKDTree Pairwise Proximity Check (1.5m / 2.0m)"]
        GeomSweep --> KinematicsCheck["Velocity, Accel, Yaw-Rate Kinematic Checks"]
        GeomSweep --> GeofenceCheck["Horizontal Geofence Check (Cylindrical r <= 30m)"]
        GeomSweep --> AltitudeCheck["Altitude Envelope Check (0.0m <= h <= 120m)"]
    end

    subgraph SimEngine ["Fleet Simulation (tara-sim / SITL)"]
        CoreValidation --> SimFleet["Synthetic Kinematic Fleet (Vectorized NumPy)"]
        SimFleet --> FaultInject["Fault Injection (RF Loss, RTK Degradation, Battery Sag)"]
        FaultInject --> SITL["PX4 SITL / Multi-Vehicle Container Simulation"]
        SITL --> SimReport["Simulation Conformance & Tracking Lag Report"]
    end
```

---

## 3. Real-Time Telemetry, Observability & Incident Replay Pipeline

The runtime monitoring, geofence interlock, and incident analysis pipeline:

```mermaid
graph TD
    subgraph FleetTelemetry ["Fleet Downlink"]
        Airborne["Airborne Drone Swarm (10-1000 Units)"] --> SlottedTDMA["Slotted TDMA / Broadcast Radio Downlink"]
    end

    subgraph GCSIngest ["GCS Ingestion & Safety Monitors"]
        SlottedTDMA --> VectorIngest["High-Rate Telemetry Stream Parser"]
        VectorIngest --> GeofenceMonitor["10Hz Geofence Monitor (r > 30m -> RTL)"]
        VectorIngest --> ProximityMonitor["10Hz Fleet Proximity Monitor (d < 1.5m -> RTL)"]
        VectorIngest --> StateMachine["Centralized Fleet State Machine"]
    end

    subgraph Observability ["Observability & Operator HUD"]
        StateMachine --> HUD["Cesium 3D EarthView / NightSkyView HUD"]
        StateMachine --> Metrics["Real-Time Signal Quality, EKF2, Battery Metrics"]
    end

    subgraph IncidentReplay ["Persistence & Incident Analysis"]
        VectorIngest --> StateSnapshot["Periodic Show-State Snapshots (tara_gcs.state)"]
        VectorIngest --> TelemetryLogger["Structured Flight Log Recorder (CSV/Parquet)"]
        TelemetryLogger --> IncidentReplayEngine["Post-Flight Incident Replay & Blackbox Analysis"]
    end
```

---

## 4. Subsystem Dependency Hierarchy

```
tara-core  (Pure mathematical, spatial safety, and contract definitions)
   ▲
   ├── tara-studio (Choreography authoring, pacing, voxelization)
   │      ▲
   │      └── tara-studio-web (React 18 / Three.js browser IDE)
   │
   ├── tara-sim (Vectorized kinematics, SITL orchestration, fault models)
   │      ▲
   │      └── tara-gcs (Ground Control Station, fleet FSM, safety interlocks)
   │             ▲
   │             └── tara-web (WebGL NightSkyView, Cesium geospatial HUD)
```
