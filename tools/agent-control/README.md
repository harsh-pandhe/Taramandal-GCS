# Agent Screen & Cursor Control Guide

This guide explains how to enable AI agents to autonomously control your mouse cursor, screen, and desktop, as well as how to run the automated recording suite for Taramandal deliverables.

---

## 1. Automated Headless / In-Browser Recording (Issue #132)

If you need a video recording of **Taramandal Studio** with full cursor interaction, tab switching, 3D canvas orbiting, safety validation checks, and timeline scrubbing, run:

```bash
node tools/record/record_studio_walkthrough.mjs
```

- **Features**:
  - Automatically launches the Studio frontend with WebGL enabled.
  - Injects a high-visibility SVG virtual mouse cursor with animated easing and click ripples.
  - Tours the 3D formation, Sequencer, Light panel, Audio panel, AI panel, Safety validation, Swarm Fleet, and Project Vault.
  - Records and renders an optimized H.264 MP4 at `docs/recordings/taramandal-studio-walkthrough.mp4`.

---

## 2. Gazebo / RViz Multi-Drone Screen Recording (Issues #171 & #172)

To record a live ROS 2 / PX4 SITL flight in Gazebo and RViz without holding the camera or manual screen recording:

```bash
# Record primary display (:0.0) for 45 seconds
./tools/record/record_desktop_gazebo.sh gazebo_flight_3drones 45 :0.0
```

- **Features**:
  - Direct hardware/software X11 grab via `ffmpeg`.
  - Captures full 1080p resolution with visible cursor movements.
  - Exports web-ready MP4 to `docs/recordings/gazebo_flight_3drones.mp4`.

---

## 3. Autonomous AI Desktop Agent Setup (Hands-Free Mouse & Keyboard)

If you want an AI agent to continuously **see your screen, move your mouse, click buttons, and run desktop software** via natural language instructions, use one of the following open-source frameworks:

### Option A: UI-TARS Desktop (ByteDance) — Recommended
A native desktop app powered by Vision-Language Models (VLM) + Reinforcement Learning designed specifically for OS/desktop control.

- **GitHub**: [bytedance/UI-TARS-desktop](https://github.com/bytedance/UI-TARS-desktop)
- **Installation**:
  ```bash
  git clone https://github.com/bytedance/UI-TARS-desktop.git
  cd UI-TARS-desktop
  npm install
  npm run dev
  ```
- **How it works**:
  - Connects to local Ollama/vLLM (e.g., `Qwen2-VL` or `UI-TARS-7B`) or remote APIs (Claude 3.5 Sonnet / GPT-4o).
  - You type a prompt: *"Open Gazebo, load the world, and click launch on GCS"*.
  - The agent takes screenshots, detects pixel targets, moves your physical mouse, and performs clicks.

---

### Option B: Simular Agent-S
State-of-the-art benchmark performer on OSWorld for complex GUI operating system tasks.

- **GitHub**: [simular-ai/Agent-S](https://github.com/simular-ai/Agent-S)
- **Installation**:
  ```bash
  git clone https://github.com/simular-ai/Agent-S.git
  cd Agent-S
  pip install -r requirements.txt
  ```
- **How it works**:
  - Employs a dual-agent structure: high-level planner + low-level visual grounder.
  - Interacts directly with native Linux X11/Wayland windows via `pyautogui` and `xdotool`.

---

### Option C: Open Interpreter
An open-source natural language interface that executes Python and shell commands locally, with computer-use support.

- **GitHub**: [openinterpreter/open-interpreter](https://github.com/openinterpreter/open-interpreter)
- **Installation**:
  ```bash
  pip install open-interpreter
  interpreter --os
  ```
- **How it works**:
  - The `--os` flag activates vision and mouse/keyboard control mode to navigate desktop applications.
