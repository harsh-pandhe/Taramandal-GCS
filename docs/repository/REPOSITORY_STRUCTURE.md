# Taramandal Public Software Repository Architecture & Layout

**Repository:** `harsh-pandhe/Taramandal-GCS`  
**Purpose:** Canonical Home of the Taramandal Drone Show Software Suite  
**Scope:** Taramandal Studio, Taramandal GCS, Core Algorithms, Simulators, and Developer Tooling  

---

## 1. Subsystem Architecture Map

The Taramandal software repository is structured as a modular monorepo cleanly separating user-facing applications, backend microservices, core mathematical algorithms, and physics simulation harnesses:

```
Taramandal-GCS/
├── apps/
│   └── tara/                       # Unified developer CLI & service orchestration
│       ├── tara_cli/               # CLI commands: up, down, status, reset, doctor
│       └── tests/                  # Integration tests for service lifecycles
│
├── packages/
│   ├── tara-core/                  # Pure math, geometry, assignment algorithms & schemas
│   │   ├── tara_core/              # Hungarian matching, cKDTree spatial safety, geofencing
│   │   └── tests/                  # Deterministic algorithmic unit tests
│   │
│   ├── tara-gcs/                   # Ground Control Station backend & flight orchestration
│   │   ├── tara_gcs/               # Fast broadcast downlink, UDP discovery, MAVLink bridges
│   │   └── tests/                  # GCS endpoint, concurrency, and telemetry unit tests
│   │
│   ├── tara-sim/                   # Vectorized NumPy swarm kinematics simulator
│   │   ├── tara_sim/               # 1000-drone vectorized step engine & system adapter
│   │   └── tests/                  # Microsecond step benchmarks & physics regression
│   │
│   ├── tara-studio/                # Choreography backend, shape generation & voxelizer
│   │   ├── tara_studio/            # Mesh voxelizer, pacing engine, Blender exporter
│   │   └── tests/                  # Generator regression & aerodynamic tests
│   │
│   ├── tara-studio-web/            # Choreography UI: 3D timeline & formation sequencer
│   │   ├── src/                    # React 18, Three.js, React Three Fiber workspace
│   │   └── public/                 # Static assets, icons, hero images
│   │
│   └── tara-web/                   # Flight Operations Cockpit & audience visualizer
│       ├── src/                    # NightSkyView (Three.js), EarthView (Cesium), HUD
│       └── e2e/                    # Playwright visual-regression test harnesses
│
├── sim/
│   ├── px4/                        # PX4 SITL multi-vehicle launch scripts & params
│   └── ros/                        # Gazebo TF bridge and RViz visualization
│
├── tools/                          # Benchmarking, profiling, and trajectory generators
│   ├── benchmark_fleet_scale.py    # 100-to-1000 drone event loop load benchmark
│   ├── calibrate_sim.py            # Latency and telemetry jitter calibration
│   └── generate_trajectory.py      # Standalone 4D trajectory generator
│
├── examples/                       # Public show choreography samples (.json)
│
├── docs/                           # Public-Safe Software Documentation
│   ├── architecture/               # High-level software roadmap & status audit
│   ├── gcs/                        # GCS technical specifications & sharded design
│   ├── studio/                     # Studio design and choreography algorithms
│   ├── simulation/                 # Simulation benchmarks and architecture
│   ├── protocols/                  # MAVLink and show schema specifications
│   ├── security/                   # Threat model and signing specifications
│   ├── testing/                    # Test strategy and CI quality gates
│   ├── repository/                 # Repository governance & structure definitions
│   └── archive/                    # Historical public-safe software notes
│
├── .github/                        # CI/CD Workflows, templates, and CODEOWNERS
│   ├── workflows/                  # ci.yml, load-scale.yml
│   └── ISSUE_TEMPLATE/             # Standardized task templates
│
├── CHANGELOG.md                    # Software release version history
├── CONTRIBUTING.md                 # Contributor guide and environment setup
├── pyproject.toml                  # Monorepo Python build configuration
├── README.md                       # Main product overview and quickstart
├── requirements.txt                # Unified Python dependency specifications
├── tara                            # Symlink / runner for CLI
└── tara.toml                       # Monorepo service ports and backend settings
```

---

## 2. Where Should I Put a New File?

To keep the repository clean, structured, and modular, all contributors must follow these placement rules:

| Contribution Type | Target Directory | Guidelines |
| :--- | :--- | :--- |
| **New GCS Backend Feature** | `packages/tara-gcs/tara_gcs/` | Fast, asynchronous, non-blocking Python logic. Add unit tests in `packages/tara-gcs/tests/`. |
| **New Studio Choreography Logic** | `packages/tara-studio/tara_studio/` | Mathematical generators, transitions, and export logic. Add tests in `packages/tara-studio/tests/`. |
| **New Studio Frontend View / Component** | `packages/tara-studio-web/src/components/` | Modular React components. Use existing design tokens. |
| **New GCS Cockpit View / HUD Panel** | `packages/tara-web/src/views/` or `src/components/` | Real-time telemetry widgets. Test rendering with Playwright. |
| **New Core Algorithm / Math Model** | `packages/tara-core/tara_core/` | Zero external framework dependencies. Pure NumPy/SciPy/standard library. |
| **New Simulator Physics / Fault Model** | `packages/tara-sim/tara_sim/` | Keep execution vectorized in NumPy arrays. Avoid per-drone Python objects in hot loops. |
| **New CLI Command / Developer Tool** | `apps/tara/tara_cli/` | Register in `apps/tara/tara_cli/__main__.py` and add lifecycle test in `apps/tara/tests/`. |
| **New Software Architecture Guide** | `docs/architecture/` or `docs/<domain>/` | High-level software designs only. Must not contain proprietary hardware/BOM details. |
| **New Sample Show Choreography** | `examples/` | Validated, collision-free `.json` or `.tarm` show files. |

---

## 3. Strict Negative Boundary: What Does NOT Belong Here

Under no circumstances should the following material be committed or placed in this repository:
- **No Physical Hardware Designs:** KiCad PCB layouts (`*.kicad_pcb`), schematics (`*.kicad_sch`), netlists, pinout tables.
- **No Mechanical CAD:** STEP files (`*.step`), SolidWorks parts, airframe 3D models, CNC routing toolpaths.
- **No Confidential Procurement or Manufacturing:** BOMs with supplier pricing, vendor quotes, assembly jig specs.
- **No RF / Antenna Engineering Research:** S-parameter files, RF impedance matching schematics, LNA noise budgets.
- **No Business / Investor Material:** Pitch decks, investor reports, financial projections, client show contracts.
- **No Secrets or Credentials:** API keys, private signing keys, production certificates, `.env` files.

*All hardware, research, manufacturing, and business materials belong exclusively in the local private engineering workspace (`/home/harsh-pandhe/Taramandal-Private`).*
