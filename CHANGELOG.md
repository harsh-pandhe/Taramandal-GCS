# Changelog

All notable changes to the Taramandal GCS and Drone Swarm Fleet project are documented in this file.

## [1.0.0] - 2026-07-06

### Added
- **Custom Gazebo Simulator World:** Created `drone_show_field.sdf` with custom aligned launch pads (`pad_0` to `pad_4`) and a visual red safety geofence ring.
- **FastAPI Ground Control Station API:** Built backend service in `backend/main.py` offering API control routes (`/api/launch`, `/api/rtl`, `/api/land`, `/api/upload-trajectory`, `/api/stop-trajectory`) and a 5Hz WebSockets telemetry feed.
- **Swarm Drone Manager:** Implemented `backend/drone_manager.py` to handle concurrent MAVSDK connection streams on isolated gRPC ports (`50051 + drone_id`) and perform linear coordinate interpolation.
- **CSV & JSON Trajectory Parser:** Implemented `backend/trajectory_parser.py` using `pandas` to read and parse flight plans.
- **Glassmorphic React Frontend Dashboard:** Created React Vite app inside `frontend/` featuring pre-flight checklist logs, battery indicators, coordinates display, and a drag-and-drop trajectory plan uploader.
- **Simulation Control Scripts:** Developed `scripts/launch_fleet.sh` (with isolated partition settings and daemon mode flags) and `scripts/stop_fleet.sh` (which stops `px4`, `gz sim`, and `mavsdk_server` processes).
- **Project Documentation:** Created detailed `README.md` (with system architecture and Gantt schedules), `roadmap.md`, and visual assets directory `docs/`.
