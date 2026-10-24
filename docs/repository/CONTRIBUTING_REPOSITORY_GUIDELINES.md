# Contributor Repository Guidelines & Public Safety Hygiene

**Repository:** `harsh-pandhe/Taramandal-GCS`  
**Audience:** All Core Developers, Contributors, and Automation Agents  
**Scope:** Repository Hygiene, Public/Private Boundary, and Code Standards  

---

## 1. Absolute Rule: The Public / Private Engineering Wall

The GitHub repository `harsh-pandhe/Taramandal-GCS` is strictly dedicated to the **software product**:
- **Taramandal Studio** (Choreography, timeline sequencing, voxelization)
- **Taramandal GCS** (Swarm command-and-control, safety monitoring, telemetry)
- **Core Shared Libraries & Simulators**

Physical hardware designs, PCB layouts, CAD files, BOMs with pricing, RF/GNSS research, and commercial/investor documents reside exclusively in the **Private Local Engineering Workspace** (`/home/harsh-pandhe/Taramandal-Private`).

---

## 2. Pre-Commit & Pre-Stage Checklist

Before staging any file into Git:

1. **Verify Working Tree Status:**
   ```bash
   git status --short
   ```
2. **Review Untracked Files:**
   Ensure no file starting with `00_*` through `23_*`, `V1_*`, `*.kicad_*`, `*.step`, or containing BOM/pricing data is listed.
3. **Never Blind-Stage:**
   - **DO NOT RUN:** `git add .` or `git add -A` from the repository root.
   - **DO RUN:** `git add packages/tara-gcs/tara_gcs/myfile.py` (explicit file paths only).
4. **Secret Scanning:**
   Never stage `.env` files, private Ed25519 signing keys, API tokens, or credentials.

---

## 3. Code Standards & Testing Policy

Every pull request must maintain high architectural rigor:
- **No Dead Code or Placeholders:** All features must include concrete, testable implementations.
- **Automated Tests Required:**
  - Python features require pytest unit tests in the corresponding `packages/<name>/tests/` directory.
  - Frontend features require Vitest unit tests or Playwright E2E visual tests.
  - Performance-critical code must adhere to sub-microsecond vectorized execution (NumPy arrays over Python loops).
- **Format & Lint:**
  - Python: Checked with `ruff` and typed with `mypy`.
  - Frontend: Checked with `oxlint`.

---

## 4. Hardware Interfaces in Software

Software may interface with physical autopilots and radios via **abstract protocols**:
- Use generic MAVLink v2 message structures (`SET_POSITION_TARGET_LOCAL_NED`, `MISSION_ITEM_INT`).
- Implement generic serial transport drivers (`SerialRadioTransport`).
- Do **not** hardcode specific proprietary RF silicon models, private PCB netlists, or workshop bench wiring procedures into public software files.
