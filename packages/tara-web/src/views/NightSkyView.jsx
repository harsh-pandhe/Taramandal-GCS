import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import { OrbitControls } from '@react-three/drei';
import { EffectComposer, Bloom } from '@react-three/postprocessing';
import * as THREE from 'three';
import { FLOATS_PER_DRONE } from '../telemetry';
import {
  BAND_COLORS,
  BAND_CRITICAL,
  BAND_NOMINAL,
  BAND_UNMONITORED,
  BAND_WARNING,
  NOMINAL_MAX_M,
  WARNING_MAX_M,
} from '../deviation';

// "How it looks IRL at night" — a ground-level spectator's view of the show,
// as opposed to the operator's top-down Earth view (Phase 4) or the flat
// telemetry dashboard. Black sky, a scattered starfield, a dark ground
// silhouette, and the fleet itself as glowing points with a real bloom pass —
// this is the one meant to look like a photo of an actual show, not a
// simulation debug view.
//
// Coordinate mapping matches the existing trajectory-preview scene elsewhere
// in the app (DronePathLine): NED -> Three.js is (east, up, north). Live
// telemetry's fleet_z is already altitude-positive-up (unlike raw trajectory
// waypoints' NED-down z), so it needs no sign flip here.
//
// Scale note: everything on the per-frame path is written for 1000 drones.
// That means no per-drone React state, no per-frame allocation, and no
// per-drone object churn — the fleet lives in a preallocated typed array and is
// pushed straight into instance attributes. Two instanced meshes (bodies +
// glow billboards) and one starfield Points is the entire draw-call budget.

const MAX_DRONES = 1000; // instanced mesh capacity ceiling, matches tara_core.layout.MAX_FLEET_SIZE-scale intent
const STAR_COUNT = 3000;
const STAR_FIELD_RADIUS = 400;

// Offsets into one fast-frame record (see ../telemetry.js FAST_FIELDS). The
// inner loop indexes the flat buffer directly, so it needs the positions as
// constants rather than field names.
const OFF_FLEET_X = 0;
const OFF_FLEET_Y = 1;
const OFF_FLEET_Z = 2;
const OFF_CONNECTED = 8;
const OFF_PLANNED_X = 9;
const OFF_PLANNED_Y = 10;
const OFF_PLANNED_Z = 11;
const OFF_PLANNED_VALID = 12;

// Critical drones pulse rather than sitting at a flat red — a moving signal is
// what actually catches a director's eye across a 1000-point sky.
const CRITICAL_FLASH_HZ = 3.0;

// The HUD is React state, but it must not re-render at frame rate; summary
// values are pushed out on this interval instead.
const SUMMARY_INTERVAL_S = 0.25;

function Starfield() {
  const positions = useMemo(() => {
    const arr = new Float32Array(STAR_COUNT * 3);
    for (let i = 0; i < STAR_COUNT; i++) {
      // Random point on a sphere shell, so stars read as a night sky dome
      // rather than a flat backdrop regardless of camera orbit.
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      const r = STAR_FIELD_RADIUS * (0.8 + Math.random() * 0.2);
      arr[i * 3] = r * Math.sin(phi) * Math.cos(theta);
      arr[i * 3 + 1] = Math.abs(r * Math.cos(phi)) * 0.6 + 20; // keep stars above the horizon
      arr[i * 3 + 2] = r * Math.sin(phi) * Math.sin(theta);
    }
    return arr;
  }, []);

  return (
    <points frustumCulled={false}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[positions, 3]} />
      </bufferGeometry>
      <pointsMaterial size={1.1} color="#cbd5e1" sizeAttenuation={false} transparent opacity={0.8} />
    </points>
  );
}

function GroundSilhouette({ radius = 60 }) {
  return (
    <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.05, 0]}>
      <circleGeometry args={[radius, 64]} />
      <meshBasicMaterial color="#04060b" />
    </mesh>
  );
}

// Two InstancedMeshes for the whole fleet — the same "one draw call regardless
// of drone count" principle as the Earth view's PointPrimitiveCollection, just
// on the Three.js side. The body mesh is the LED itself; the glow mesh is a
// camera-facing billboard quad per drone carrying an additive radial falloff,
// which is what sells the 360-degree LED glow without paying for real
// volumetric geometry.
//
// Position/colour are pushed straight into the instance buffers in useFrame
// rather than through React state, so 1000 drones updating at telemetry rate
// doesn't mean 1000 re-renders/frame.
function FleetPoints({ fleetRef, onSummary }) {
  const bodyRef = useRef(null);
  const glowRef = useRef(null);
  const { camera } = useThree();

  // Every mutable per-frame object is allocated once here, never inside
  // useFrame — at 60 FPS a single `new THREE.Color()` in the inner loop is
  // 60000 allocations/second at full fleet size.
  const scratch = useMemo(
    () => ({
      dummy: new THREE.Object3D(),
      color: new THREE.Color(),
      // Counters reused across frames so the HUD summary costs no allocation.
      counts: {
        [BAND_UNMONITORED]: 0,
        [BAND_NOMINAL]: 0,
        [BAND_WARNING]: 0,
        [BAND_CRITICAL]: 0,
      },
    }),
    [],
  );

  // The glow billboard's radial-falloff texture, generated once on the CPU.
  // A 64x64 canvas is plenty — it's magnified across a screen-space quad and
  // then bloomed, so texel detail is invisible but the memory saving at
  // 1000 instances is not.
  const glowTexture = useMemo(() => {
    const size = 64;
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext('2d');
    const gradient = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    gradient.addColorStop(0.0, 'rgba(255,255,255,1.0)');
    gradient.addColorStop(0.25, 'rgba(255,255,255,0.55)');
    gradient.addColorStop(1.0, 'rgba(255,255,255,0.0)');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, size, size);
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    return texture;
  }, []);

  // Three.js keeps GPU resources alive until they're explicitly disposed;
  // without this the texture leaks every time the view unmounts, which is
  // precisely what the 15-minute memory-footprint criterion would catch.
  useEffect(() => () => glowTexture.dispose(), [glowTexture]);

  const lastSummaryAt = useRef(0);

  useFrame((state) => {
    const body = bodyRef.current;
    const glow = glowRef.current;
    if (!body || !glow) return;

    const fleet = fleetRef.current;
    const data = fleet.data;
    const n = Math.min(fleet.count, MAX_DRONES);
    const { dummy, color, counts } = scratch;
    const time = state.clock.elapsedTime;

    // A single flash factor for the whole frame — every critical drone pulses
    // in phase, which reads as an alarm rather than as visual noise.
    const flash = 0.55 + 0.45 * Math.sin(time * Math.PI * 2 * CRITICAL_FLASH_HZ);

    counts[BAND_UNMONITORED] = 0;
    counts[BAND_NOMINAL] = 0;
    counts[BAND_WARNING] = 0;
    counts[BAND_CRITICAL] = 0;
    let worstError = 0;
    let worstId = null;

    for (let i = 0; i < n; i++) {
      const base = i * FLOATS_PER_DRONE;
      const fleetX = data[base + OFF_FLEET_X];
      const fleetY = data[base + OFF_FLEET_Y];
      const fleetZ = data[base + OFF_FLEET_Z];
      const connected = data[base + OFF_CONNECTED] !== 0;
      const plannedValid = data[base + OFF_PLANNED_VALID] !== 0;

      // Deviation is computed inline off the flat buffer rather than through
      // deviation.js's object API: the shared module owns the thresholds
      // (imported above, not re-typed here), but building 1000 wrapper objects
      // per frame just to call it would defeat the whole typed-array path.
      let error = 0;
      let band;
      if (!connected || !plannedValid) {
        band = BAND_UNMONITORED;
      } else {
        const dx = fleetX - data[base + OFF_PLANNED_X];
        const dy = fleetY - data[base + OFF_PLANNED_Y];
        const dz = fleetZ - data[base + OFF_PLANNED_Z];
        error = Math.sqrt(dx * dx + dy * dy + dz * dz);
        if (error < NOMINAL_MAX_M) band = BAND_NOMINAL;
        else if (error <= WARNING_MAX_M) band = BAND_WARNING;
        else band = BAND_CRITICAL;
      }

      counts[band] += 1;
      if (band !== BAND_UNMONITORED && error > worstError) {
        worstError = error;
        worstId = i;
      }

      // NED fleet frame -> Three.js: (east, up, north).
      dummy.position.set(fleetY, fleetZ, fleetX);
      dummy.rotation.set(0, 0, 0);
      dummy.scale.setScalar(1);
      dummy.updateMatrix();
      body.setMatrixAt(i, dummy.matrix);

      const rgb = BAND_COLORS[band];
      const gain = band === BAND_CRITICAL ? flash : 1.0;
      color.setRGB(rgb[0] * gain, rgb[1] * gain, rgb[2] * gain);
      body.setColorAt(i, color);

      // Glow billboards face the camera each frame. Critical drones get a
      // wider halo on top of the flash so they stay findable even when the
      // fleet is dense enough that individual LEDs overlap.
      dummy.scale.setScalar(band === BAND_CRITICAL ? 3.4 : 2.0);
      dummy.quaternion.copy(camera.quaternion);
      dummy.updateMatrix();
      glow.setMatrixAt(i, dummy.matrix);
      color.multiplyScalar(band === BAND_CRITICAL ? 0.85 : 0.45);
      glow.setColorAt(i, color);
    }

    // Instances beyond the current fleet size collapse to a degenerate
    // (zero-scale) matrix so a shrinking fleet doesn't leave stale points
    // floating at their last known position.
    dummy.scale.set(0, 0, 0);
    dummy.rotation.set(0, 0, 0);
    dummy.updateMatrix();
    for (let i = n; i < MAX_DRONES; i++) {
      body.setMatrixAt(i, dummy.matrix);
      glow.setMatrixAt(i, dummy.matrix);
    }

    body.instanceMatrix.needsUpdate = true;
    glow.instanceMatrix.needsUpdate = true;
    if (body.instanceColor) body.instanceColor.needsUpdate = true;
    if (glow.instanceColor) glow.instanceColor.needsUpdate = true;

    // Drawing only the live range keeps the vertex cost proportional to the
    // actual fleet rather than to the 1000-instance capacity.
    body.count = n;
    glow.count = n;

    if (time - lastSummaryAt.current >= SUMMARY_INTERVAL_S) {
      lastSummaryAt.current = time;
      onSummary({
        total: n,
        nominal: counts[BAND_NOMINAL],
        warning: counts[BAND_WARNING],
        critical: counts[BAND_CRITICAL],
        unmonitored: counts[BAND_UNMONITORED],
        worstError,
        worstId,
      });
    }
  });

  return (
    <>
      <instancedMesh ref={bodyRef} args={[undefined, undefined, MAX_DRONES]} frustumCulled={false}>
        <sphereGeometry args={[0.35, 8, 8]} />
        <meshBasicMaterial toneMapped={false} />
      </instancedMesh>

      <instancedMesh ref={glowRef} args={[undefined, undefined, MAX_DRONES]} frustumCulled={false}>
        <planeGeometry args={[1, 1]} />
        <meshBasicMaterial
          map={glowTexture}
          toneMapped={false}
          transparent
          blending={THREE.AdditiveBlending}
          depthWrite={false}
        />
      </instancedMesh>
    </>
  );
}

function DeviationHUD({ summary }) {
  const rows = [
    { label: 'NOMINAL', key: 'nominal', color: '#22e08a', hint: `< ${NOMINAL_MAX_M}m` },
    { label: 'WARNING', key: 'warning', color: '#fbbf24', hint: `${NOMINAL_MAX_M}–${WARNING_MAX_M}m` },
    { label: 'CRITICAL', key: 'critical', color: '#f43f5e', hint: `> ${WARNING_MAX_M}m` },
  ];

  return (
    <div className="nightsky-hud" data-testid="deviation-hud">
      <div className="nightsky-hud-title">TRAJECTORY DEVIATION</div>
      {rows.map((row) => (
        <div className="nightsky-hud-row" key={row.key}>
          <span className="nightsky-hud-dot" style={{ background: row.color }} />
          <span className="nightsky-hud-label">{row.label}</span>
          <span className="nightsky-hud-hint">{row.hint}</span>
          <span
            className="nightsky-hud-count"
            style={{ color: row.color }}
            data-testid={`hud-${row.key}`}
          >
            {summary[row.key]}
          </span>
        </div>
      ))}
      <div className="nightsky-hud-footer">
        <span data-testid="hud-tracked">
          TRACKED {summary.total - summary.unmonitored}/{summary.total}
        </span>
        <span data-testid="hud-worst">
          MAX ERR {summary.worstError.toFixed(2)}m
          {summary.worstId !== null ? ` · D${summary.worstId}` : ''}
        </span>
      </div>
      {summary.critical > 0 && (
        <div className="nightsky-hud-alert" data-testid="hud-critical-alert">
          ⚠ {summary.critical} DRONE{summary.critical === 1 ? '' : 'S'} OFF TRAJECTORY
        </div>
      )}
    </div>
  );
}

const EMPTY_SUMMARY = {
  total: 0,
  nominal: 0,
  warning: 0,
  critical: 0,
  unmonitored: 0,
  worstError: 0,
  worstId: null,
};

export default function NightSkyView({ telemetry, fleetBuffer }) {
  const [summary, setSummary] = useState(EMPTY_SUMMARY);

  // The fleet lives in one preallocated Float32Array for the lifetime of the
  // view. When App streams the raw binary frame through `fleetBuffer` we point
  // straight at it (zero copy, zero parse); otherwise we transcode the
  // object-shaped `telemetry` prop into the same layout, so this view works
  // unchanged against either source.
  const localFleet = useRef(null);
  if (localFleet.current === null) {
    localFleet.current = { data: new Float32Array(MAX_DRONES * FLOATS_PER_DRONE), count: 0 };
  }
  const fleetRef = useRef(localFleet.current);

  if (fleetBuffer && fleetBuffer.current) {
    fleetRef.current = fleetBuffer.current;
  } else {
    const fleet = localFleet.current;
    const ids = telemetry ? Object.keys(telemetry) : [];
    const n = Math.min(ids.length, MAX_DRONES);
    for (let i = 0; i < n; i++) {
      const drone = telemetry[ids[i]];
      const base = i * FLOATS_PER_DRONE;
      fleet.data[base + OFF_FLEET_X] = drone.fleet_x || 0;
      fleet.data[base + OFF_FLEET_Y] = drone.fleet_y || 0;
      fleet.data[base + OFF_FLEET_Z] = drone.fleet_z || 0;
      fleet.data[base + OFF_CONNECTED] = drone.connected ? 1 : 0;
      fleet.data[base + OFF_PLANNED_X] = drone.planned_x || 0;
      fleet.data[base + OFF_PLANNED_Y] = drone.planned_y || 0;
      fleet.data[base + OFF_PLANNED_Z] = drone.planned_z || 0;
      fleet.data[base + OFF_PLANNED_VALID] = drone.planned_valid ? 1 : 0;
    }
    fleet.count = n;
    fleetRef.current = fleet;
  }

  // Summary updates arrive from inside the render loop; drop the ones that
  // wouldn't change a pixel of the HUD so a steady show doesn't re-render
  // React four times a second for nothing.
  const summaryRef = useRef(EMPTY_SUMMARY);
  const handleSummary = useMemo(
    () => (next) => {
      const prev = summaryRef.current;
      if (
        prev.total === next.total &&
        prev.nominal === next.nominal &&
        prev.warning === next.warning &&
        prev.critical === next.critical &&
        prev.unmonitored === next.unmonitored &&
        Math.abs(prev.worstError - next.worstError) < 0.01
      ) {
        return;
      }
      summaryRef.current = next;
      setSummary(next);
    },
    [],
  );

  return (
    <div className="nightsky-root">
      <Canvas
        camera={{ position: [0, 4, 45], fov: 50 }}
        // Antialiasing is redundant once the bloom pass is doing the smoothing,
        // and at 1000 instances the MSAA buffer is real memory against the
        // 150MB budget. dpr is capped for the same reason: a 3x retina display
        // would otherwise quadruple the post-processing fill cost.
        gl={{ antialias: false, powerPreference: 'high-performance' }}
        dpr={[1, 1.75]}
        style={{ background: '#000000' }}
      >
        <fog attach="fog" args={['#000814', 40, 320]} />
        <ambientLight intensity={0.15} />

        <Starfield />
        <GroundSilhouette radius={60} />
        <FleetPoints fleetRef={fleetRef} onSummary={handleSummary} />

        <OrbitControls
          target={[0, 5, 0]}
          minDistance={5}
          maxDistance={250}
          maxPolarAngle={Math.PI / 2 + 0.15}
        />

        <EffectComposer>
          <Bloom luminanceThreshold={0.15} luminanceSmoothing={0.6} intensity={1.4} mipmapBlur />
        </EffectComposer>
      </Canvas>

      <DeviationHUD summary={summary} />
    </div>
  );
}
