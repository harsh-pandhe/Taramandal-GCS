# Replay performance at 100 drones x 15 minutes (issue #65)

Measured 14 Nov 2026 on one laptop: first on `main` at `d06cdae` (the replay tab, #271), then again with the full-rate zoom
(see "Full-rate zoom" below). This is the evidence for the issue's
"scrubs smoothly at 60 fps" line, for **this machine and this size only**. Read the limits below before quoting it.

## How to reproduce

```
python tools/replay_perf_record.py --out /some/dir        # records the flight, prints the API numbers, writes overview.json
cd packages/tara-web
REPLAY_PERF_DATA=/some/dir/overview.json REPLAY_PERF_WINDOW=/some/dir/window30.json \
  npx playwright test e2e/replay-perf.spec.js
```

The recording is made by the real black-box recorder against the real `tara_sim` physics, the same way
`test_15_minute_100_drone_log_is_under_25_mb` does it, plus a synthetic breach and clear every 3 minutes so the timeline
has markers. The Playwright spec is skipped unless `REPLAY_PERF_DATA` is set, so it does not run in CI.

## Results

| | Measured |
|---|---|
| Recording size, 15 min x 100 drones, 9,000 ticks | 3.11 MB (budget 25 MB) |
| Decode of the whole recording on the GCS | 0.89 s |
| Default overview the browser loads (500 frames, one per 1.8 s, `stride` 18) | 4.36 MB of JSON, built in 32 ms |
| A 30 s window at the full 10 Hz (301 frames) | 2.64 MB of JSON, 49 ms |
| Browser: LOAD click to timeline shown, API mocked from a file | 0.67 s |
| Buffer fill for one displayed frame, 100 drones (`fillFleetBuffer`) | 0.009 ms per call |
| Frame cadence, replay loaded and idle (4 s) | 60 fps, p99 16.8 ms, worst 16.8 ms |
| Frame cadence while playing at 10x (6 s) | 60 fps, p99 16.8 ms, worst 16.8 ms |
| Frame cadence while scrubbing across the whole timeline (6 s) | 60 fps, p99 16.8 ms, worst 16.8 ms |

Renderer reported by the browser: `ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 5050 Laptop GPU/PCIe/SSE2, OpenGL 4.5.0)`,
headless Chromium from Playwright. Not software GL.

## Full-rate zoom

The overview is one frame per 1.8 s at this size, so while you scrub or play the replay also loads a window of every recorded
tick (10 Hz) around the current time and draws from it instead; the overview keeps drawing while a window loads, or if one fails.
The window is sized so frames x drones stays near 30,000 samples: 30 s at 100 drones, 3 s at 1,000, never more than 60 s.

| | Measured (100 drones, real API, the 15-minute recording) |
|---|---|
| One 30 s window at 10 Hz | 301 frames, stride 1, 2.24 MB over HTTP (2.64 MB as `json.dumps` text) |
| `JSON.parse` of that window (Node/V8, 30 runs) | median 8.7 ms, p90 10.2 ms, max 11.4 ms, on the main thread |
| Frame cadence while 4 such windows were fetched, parsed and swapped during playback and scrubbing | 60 fps, p99 16.8 ms, worst 16.8 ms |

The parse is close to half a frame, so a window swap could cost a dropped frame on a slower machine; none was seen here, and
moving the parse to a worker is the fix if one shows up. In this measurement the mocked GCS served the recorded window shifted
to the time asked for, so the 100 drones moved as recorded but each window's content was reused. Requests that fail are not
retried for 5 s.

## What this does and does not show

- **It shows** that on this GPU, with a 100-drone 15-minute recording loaded, the page kept every animation frame at the
  display's 60 Hz while idle, playing and scrubbing, and that the replay's own per-frame work (filling the buffer) is
  about 9 microseconds.
- **It cannot show headroom.** The frame rate is capped by the 60 Hz display, so "60 fps, worst frame 16.8 ms" means no frame
  was dropped, not that the renderer could do more. GPU time per frame was not measured.
- **One machine, one GPU.** A different or weaker GPU may behave differently. `performance.spec.js` makes the same point for the
  live view and skips its frame-rate assertion on software rendering for the same reason.
- **The scrub is synthetic.** It sets the range input and fires an `input` event on every animation frame, sweeping the whole
  timeline every 3 s. A person dragging a slider is similar but not identical.
- **The API was mocked from a file**, so the 0.67 s load time leaves out real network and the GCS's own 0.89 s decode of a
  recording it has not read yet (it caches a decoded session afterwards).
- **100 drones only.** Nothing here covers 1,000 drones. The overview budget (50,000 samples) would thin a 1,000-drone
  15-minute flight to 50 frames, about one per 18 s, which would look coarse.
- **The overview alone is coarse** (one frame per 1.8 s, blended linearly between samples, so a fast manoeuvre is smoothed). The
  full-rate window fixes that around the current time; the 1,000-drone case (3 s windows, 30 frames) was not measured here.
