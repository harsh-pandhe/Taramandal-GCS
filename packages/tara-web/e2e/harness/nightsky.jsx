// Test harness: mounts the real NightSkyView against a scriptable fleet buffer
// instead of a live WebSocket.
//
// The alternative — driving these tests through the full dashboard against a
// running GCS — would make the assertions depend on simulator dynamics, which
// is exactly the wrong thing for a visual regression test: "did the drone turn
// red" must fail because the colour mapping broke, not because the sim happened
// to fly a tighter trajectory that run. Here the harness sets the actual and
// planned positions directly, so a deviation of exactly 0.9m is exactly 0.9m.
//
// The component under test is the real one, imported unmodified — only its
// telemetry source is substituted.

import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import NightSkyView from '../../src/views/NightSkyView';
import { FLOATS_PER_DRONE } from '../../src/telemetry';

const MAX_DRONES = 1000;

// Offsets mirror FAST_FIELDS (see src/telemetry.js).
const OFF_FLEET_X = 0;
const OFF_FLEET_Y = 1;
const OFF_FLEET_Z = 2;
const OFF_CONNECTED = 8;
const OFF_PLANNED_X = 9;
const OFF_PLANNED_Y = 10;
const OFF_PLANNED_Z = 11;
const OFF_PLANNED_VALID = 12;

function Harness() {
  const fleetRef = useRef({
    data: new Float32Array(MAX_DRONES * FLOATS_PER_DRONE),
    count: 0,
  });
  const [ready, setReady] = useState(false);
  // 'always' (NightSkyView's own default, matching production) until a
  // visual-regression test freezes the scene for a deterministic screenshot.
  const [frameloop, setFrameloop] = useState('always');

  useEffect(() => {
    const fleet = fleetRef.current;

    // Places `count` drones on a deterministic grid, each sitting exactly
    // `deviations[i]` metres off its planned point along +Z (altitude). A
    // per-drone deviation array is what lets one test assert all three colour
    // bands in a single frame.
    function setFleet({ count, deviations = [], connected = true }) {
      for (let i = 0; i < count; i++) {
        const base = i * FLOATS_PER_DRONE;
        // Spread the fleet over a grid so drones don't overlap on screen.
        const col = i % 32;
        const row = Math.floor(i / 32);
        const plannedX = (row - 8) * 2.0;
        const plannedY = (col - 16) * 2.0;
        const plannedZ = 18.0;
        const deviation = deviations[i] ?? 0;

        fleet.data[base + OFF_FLEET_X] = plannedX;
        fleet.data[base + OFF_FLEET_Y] = plannedY;
        fleet.data[base + OFF_FLEET_Z] = plannedZ + deviation;
        fleet.data[base + OFF_CONNECTED] = connected ? 1 : 0;
        fleet.data[base + OFF_PLANNED_X] = plannedX;
        fleet.data[base + OFF_PLANNED_Y] = plannedY;
        fleet.data[base + OFF_PLANNED_Z] = plannedZ;
        fleet.data[base + OFF_PLANNED_VALID] = 1;
      }
      fleet.count = count;
    }

    // Marks every drone as having no active trajectory — the "parked fleet"
    // case that must read as unmonitored rather than as perfectly nominal.
    function clearPlan() {
      for (let i = 0; i < fleet.count; i++) {
        fleet.data[i * FLOATS_PER_DRONE + OFF_PLANNED_VALID] = 0;
      }
    }

    // Stops NightSkyView's Canvas repainting so a visual-regression test can
    // capture one bit-stable frame. The scene keeps painting on 'always' (the
    // default) for every other test here, which need the render loop live to
    // reflect setFleet/clearPlan without an explicit invalidate() call; the
    // 2 visual-regression tests call this immediately before toHaveScreenshot,
    // after the fleet/HUD state they're capturing has already settled.
    // React Three Fiber's frameloop switch takes effect from the render this
    // triggers onward — not synchronously — so callers must still wait a
    // couple of animation frames before treating the canvas as frozen (see
    // the visual-regression tests in deviation.spec.js).
    function freeze() {
      setFrameloop('demand');
    }

    window.__nightsky = { setFleet, clearPlan, freeze, fleet };
    setReady(true);
  }, []);

  if (!ready) return null;

  return (
    <div style={{ width: '100vw', height: '100vh' }}>
      <NightSkyView fleetBuffer={fleetRef} frameloop={frameloop} />
    </div>
  );
}

// Opt out of hot module replacement. These specs run measurements lasting
// minutes; an HMR reload part-way through would swap the harness out from under
// the run and produce a failure that looks like a leak or a crash rather than
// like an edit landing mid-test.
if (import.meta.hot) import.meta.hot.decline();

createRoot(document.getElementById('root')).render(<Harness />);
