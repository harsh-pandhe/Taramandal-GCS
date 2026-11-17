# Asset and music provenance: what the repo can and cannot state (#134 D7)

Written 2026-11-17 from `main` at `db0e2b5`. This is an inventory, not a clearance. It lists what the repository itself records about where each asset came from and under what terms, and what is **not recorded**, so that a rights holder or Harsh can fill the gaps. Nothing here grants or confirms a licence.

## 1. The music track: nothing exists

| Question | What the repo says |
|---|---|
| Is any audio file tracked? | **No.** `git ls-files` shows no `.mp3`, `.wav`, `.ogg`, `.flac`, `.m4a`, `.aac` file. |
| Does any recording carry audio? | **No.** Every clip in `docs/recordings/` is silent (the notes files list "audio" under *not in clip*). |
| What does the code do with music? | `packages/tara-studio/tara_studio/music_analysis.py` analyses a file the user supplies (and the upload is size-limited and validated, PR #239). No track ships with the product. |
| Who chooses the track? | Nobody yet. The checklist and runbook say a silent clip is the default and Harsh approves the source (`docs/recording-checklist.md`, `docs/diwali-runbook.md` 4.3). |

**To close D7 for music, the rights holder needs to give us, per track:** title; artist or composer; the rights holder(s) (composition and recording are often different people); the licence or written permission and a copy of it; whether it covers **public performance** at the show, **synchronisation** with the show video, and **recording and sharing** that video; territory (India only, or worldwide); term (dates); whether attribution text is required and its exact wording; who signed for the licensor and for us; and the file's exact name and checksum so the licence is tied to the file used.

## 2. Original work by the team (provenance known from git)

| Asset | Where | First commit | Note |
|---|---|---|---|
| Diwali storyboard shapes: `diwali.svg`, `happy.svg` (letter contours), `rangoli.svg`, `diya_festive.svg` | `examples/diwali/` | `8a243e9`, Dhiraj, 2026-11-01 | Geometry committed by Dhiraj for this project (the SVG headers describe them as letter contours, a rangoli outline and a diya "from `diya_flame.svg`"; how each was produced is not recorded). The licence on them is whatever the repo's licence is, and **the repo has none** (section 4). |
| Preview renders `figures_*.png` | `examples/diwali/previews/` | `8a243e9`, Dhiraj | Renders of the project's own shapes. |
| Test fixtures `diya_flame.svg`, `dandiya_sticks.svg` | `packages/tara-studio-web/src/engine/__fixtures__/svg_assets/` | `59ad0e3`, Harsh, 2026-10-28 | Headers: "Festival Diya Oil Lamp with Flame", "Dandiya Nights Crossed Sticks Silhouette". Whether Harsh drew them or took them from somewhere is **not recorded**. |
| Letter shapes for text formations | the `GLYPHS` table in `packages/tara-studio-web/src/engine/formations.js` | in git history | A segment table in the project's own code, no font file. Lettering does not depend on any typeface. |
| Studio and dashboard favicon | `packages/tara-studio-web/public/favicon.svg`, `packages/tara-web/public/favicon.svg` | `7bbf574`, Harsh, 2026-08-02 | Origin not recorded (a drawn orbit-ring mark; presumably the team's own, unconfirmed). |

## 3. Assets whose origin the repo does not record

| Asset | Where | First commit | What is unknown |
|---|---|---|---|
| `hero.png` (identical file in both web packages) | `packages/tara-studio-web/src/assets/`, `packages/tara-web/src/assets/` | `7bbf574`, Harsh, 2026-08-02 | Author, source, licence. It arrived in the monorepo consolidation commit. A search of `src/`, `index.html` and `public/` finds no reference to it. |
| `react.svg`, `vite.svg` | both web packages' `src/assets/` | `7bbf574` | Names match the standard Vite/React starter files, but nothing in the repo says so. Not verified. No reference to either in `src/`, `index.html` or `public/`. |
| `icons.svg` (social-media icon sprite) | both web packages' `public/` | `7bbf574` | Source and licence. It contains third-party brand icons (the first symbol is `bluesky-icon`). No reference to it in `src/`, `index.html` or `public/`, so it looks unused; if so, deleting it removes the question. |
| `gcs_dashboard_mockup.jpg`, `gz_simulation_screenshot.png` | `docs/` | `c49e775`, Harsh, 2026-07-06 | Author and tool. The screenshot shows Gazebo and the mockup shows a dashboard design; whether either contains third-party artwork is not recorded. |

## 4. The repository's own licence: none

There is **no `LICENSE` file** in the repository root, and no `license` field in `pyproject.toml` or in either web `package.json`. No README or CONTRIBUTING section states a licence. Under default copyright law that means nobody outside the owners has a right to use, copy or modify the code or the shapes in section 2. Harsh (the owner) has to decide this before the repo or its shapes are shared outside the team (Yukti, IIC or others), and it decides what any attribution to others can be combined with.

## 5. Third-party material the product uses

**Dependency licences, read from the installed packages on 2026-11-17** (the licence a package declares, not a legal review):

- Python (`requirements.txt`, 21 packages, in the repo's `.venv`): permissive only. fastapi MIT; uvicorn, websockets, contourpy, psutil, pandas, httpx2 BSD-3-Clause; pydantic, svgpathtools, trimesh, typer, jsonschema, pytest MIT; python-multipart and pytest-asyncio Apache-2.0; cryptography Apache-2.0 OR BSD-3-Clause; mavsdk BSD; numpy BSD-3-Clause with 0BSD, MIT, Zlib parts; pillow MIT-CMU. scipy and matplotlib declare their own licence texts (BSD-style); not parsed here.
- npm, Studio web: 9 MIT, lucide-react ISC, `@playwright/test` Apache-2.0.
- npm, GCS dashboard: 12 MIT, cesium and `@playwright/test` Apache-2.0, postprocessing Zlib.
- Not checked here: licences of transitive dependencies; PX4, Gazebo, ROS 2 and RViz (used in simulation, not shipped in this repo); the AI model (Qwen2.5-7B, which `docs/evidence-pack.md` records as Apache-2.0, not re-checked here).

**Fonts.** `packages/tara-studio-web/index.html` loads **Archivo** and **JetBrains Mono** from Google Fonts at runtime. Both are published on Google Fonts under the SIL Open Font License (this is general knowledge, not checked in the repo; confirm on the font pages). Two consequences: no font file is in the repo, and a Studio PC with no internet falls back to system fonts, so its screen looks different from the recorded clips.

**Reference photos for the 10-drone T2 clip.** The clip's notes say the three 3D formations were designed from Wikimedia Commons photos (diya and lantern CC BY-SA 4.0, lotus CC0) and that the licences are in `SOURCES.json` in a scratch rig. That file is **not in the repo** (no `SOURCES*`, `ATTRIB*`, `NOTICE*` or `THIRD*` file is tracked), the photos are not in the repo, and the three shapes need a local Studio patch so they are not reproducible from the repo. Whether a formation designed from a CC BY-SA photo needs attribution or share-alike is an open question for the rights holder; if the clip is used outside the team, name the photographers and links.

## 6. What this changes in the evidence pack

D7 moves from MISSING to PARTIAL: the inventory exists and says plainly what is known. It stays open because no music track exists, the repository has no licence, and several assets have no recorded source. Owners: Harsh (repo licence, music source approval); a person who knows the origin of `hero.png`, `icons.svg`, the two `docs/` images and the fixture SVGs.
