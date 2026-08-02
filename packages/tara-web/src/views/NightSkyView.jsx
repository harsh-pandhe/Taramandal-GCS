import React, { useMemo, useRef } from 'react';
import { Canvas, useFrame } from '@react-three/fiber';
import { OrbitControls } from '@react-three/drei';
import { EffectComposer, Bloom } from '@react-three/postprocessing';
import * as THREE from 'three';

// "How it looks IRL at night" — a ground-level spectator's view of the show,
// as opposed to the operator's top-down Earth view (Phase 4) or the flat
// telemetry dashboard. Black sky, a scattered starfield, a dark ground
// silhouette, and the fleet itself as glowing points with a real bloom pass —
// this is the one meant to look like a photo of an actual show, not a
// simulation debug view.
//
// Coordinate mapping matches the existing trajectory-preview scene elsewhere
// in this file (DronePathLine): NED -> Three.js is (east, up, north). Live
// telemetry's fleet_z is already altitude-positive-up (unlike raw trajectory
// waypoints' NED-down z), so it needs no sign flip here.

const MAX_DRONES = 1000; // instanced mesh capacity ceiling, matches tara_core.layout.MAX_FLEET_SIZE-scale intent
const STAR_COUNT = 3000;
const STAR_FIELD_RADIUS = 400;

function statusColor(drone) {
  if (!drone || !drone.connected) return new THREE.Color('#5b6577');
  if (drone.armed) return new THREE.Color('#fbbf24');
  return new THREE.Color('#34d399');
}

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
    <points>
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

// One InstancedMesh for the whole fleet — the same "one draw call regardless
// of drone count" principle as the Earth view's PointPrimitiveCollection,
// just on the Three.js side. Position/color are pushed straight into the
// instance buffers in useFrame rather than through React state, so 100+
// drones updating at telemetry rate doesn't mean 100+ re-renders/frame.
function FleetPoints({ telemetryRef }) {
  const meshRef = useRef(null);
  const dummy = useMemo(() => new THREE.Object3D(), []);

  useFrame(() => {
    const mesh = meshRef.current;
    if (!mesh) return;
    const telemetry = telemetryRef.current;
    const ids = Object.keys(telemetry);
    const n = Math.min(ids.length, MAX_DRONES);
    for (let i = 0; i < n; i++) {
      const drone = telemetry[ids[i]];
      dummy.position.set(drone.fleet_y || 0, drone.fleet_z || 0, drone.fleet_x || 0);
      dummy.updateMatrix();
      mesh.setMatrixAt(i, dummy.matrix);
      mesh.setColorAt(i, statusColor(drone));
    }
    // Instances beyond the current fleet size collapse to a degenerate
    // (zero-scale) matrix so a shrinking fleet doesn't leave stale points
    // floating at their last known position.
    dummy.scale.set(0, 0, 0);
    dummy.updateMatrix();
    for (let i = n; i < MAX_DRONES; i++) {
      mesh.setMatrixAt(i, dummy.matrix);
    }
    dummy.scale.set(1, 1, 1);
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
  });

  return (
    <instancedMesh ref={meshRef} args={[null, null, MAX_DRONES]}>
      <sphereGeometry args={[0.35, 12, 12]} />
      <meshStandardMaterial toneMapped={false} emissiveIntensity={2.5} emissive="#ffffff" color="#ffffff" />
    </instancedMesh>
  );
}

export default function NightSkyView({ telemetry }) {
  // A ref mirror of the telemetry prop: useFrame runs every animation frame
  // (independent of React's render cycle) and needs the latest value without
  // FleetPoints re-subscribing/re-rendering on every tick.
  const telemetryRef = useRef(telemetry);
  telemetryRef.current = telemetry;

  return (
    <Canvas
      camera={{ position: [0, 4, 45], fov: 50 }}
      gl={{ antialias: true }}
      style={{ background: '#000000' }}
    >
      <fog attach="fog" args={['#000814', 40, 320]} />
      <ambientLight intensity={0.15} />

      <Starfield />
      <GroundSilhouette radius={60} />
      <FleetPoints telemetryRef={telemetryRef} />

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
  );
}
