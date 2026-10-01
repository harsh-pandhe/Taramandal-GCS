import { useMemo } from 'react';
import { Canvas } from '@react-three/fiber';
import { OrbitControls, Line, Text, Grid } from '@react-three/drei';

// ─── 3D Swarm Path Previewer (Three.js via react-three-fiber) ────────────────

const DRONE_COLORS = ['#38bdf8', '#8b5cf6', '#f2a93b', '#34d399', '#f87171'];

function DronePathLine({ waypoints, color }) {
  // NED → Three.js axes: X=East(y_NED), Y=Up(-z_NED), Z=North(x_NED)
  const points = useMemo(() =>
    waypoints.map(wp => [wp.y, -wp.z, wp.x]),
    [waypoints]
  );
  if (points.length < 2) return null;
  return (
    <>
      <Line points={points} color={color} lineWidth={2.5} />
      {points.map((pt, i) => (
        <mesh key={i} position={pt}>
          <sphereGeometry args={[i === 0 || i === points.length - 1 ? 0.28 : 0.13, 10, 10]} />
          <meshStandardMaterial color={color} emissive={color} emissiveIntensity={i === 0 ? 1.5 : 0.5} />
        </mesh>
      ))}
    </>
  );
}

function GeofenceRing({ radius = 30 }) {
  const pts = useMemo(() => {
    const arr = [];
    for (let i = 0; i <= 80; i++) {
      const a = (i / 80) * Math.PI * 2;
      arr.push([Math.sin(a) * radius, 0.05, Math.cos(a) * radius]);
    }
    return arr;
  }, [radius]);
  return <Line points={pts} color="#FF3D00" lineWidth={1.2} />;
}

/** Orbitable 3D preview of the loaded trajectory's per-drone paths. */
export default function SwarmPathPreview({ trajectoryData, geofenceRadiusM = 30.0 }) {
  const droneEntries = useMemo(() => {
    if (!trajectoryData) return [];
    return Object.keys(trajectoryData)
      .map((id, idx) => ({ id, waypoints: trajectoryData[id], color: DRONE_COLORS[idx % DRONE_COLORS.length] }))
      .filter(d => d.waypoints && d.waypoints.length > 0);
  }, [trajectoryData]);

  const cameraPos = useMemo(() => {
    if (!trajectoryData) return [15, 12, 15];
    let maxR = 5;
    Object.values(trajectoryData).forEach(wps =>
      wps.forEach(wp => {
        const r = Math.sqrt(wp.x ** 2 + wp.y ** 2 + wp.z ** 2);
        if (r > maxR) maxR = r;
      })
    );
    const d = Math.min(maxR * 2.2, 45);
    return [d, d * 0.7, d];
  }, [trajectoryData]);

  return (
    <div style={{ marginTop: '1rem', border: '1px solid var(--border)', borderRadius: 'var(--r-lg)', background: 'var(--bg-sunken)', overflow: 'hidden' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '0.55rem 0.75rem', borderBottom: '1px solid var(--border)' }}>
        <span style={{ fontSize: '0.7rem', color: 'var(--text-faint)', textTransform: 'uppercase', letterSpacing: '0.7px', fontFamily: 'var(--font-mono)' }}>3D Swarm Path Preview</span>
        <span style={{ fontSize: '0.65rem', color: 'var(--text-faint)', fontFamily: 'var(--font-mono)' }}>Drag · Orbit · Scroll to zoom</span>
      </div>

      {!trajectoryData ? (
        <div style={{ height: '220px', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'rgba(255,255,255,0.18)', fontSize: '0.8rem', flexDirection: 'column', gap: '0.4rem' }}>
          
          Upload a trajectory to preview paths in 3D
        </div>
      ) : (
        <div style={{ height: '260px' }}>
          <Canvas camera={{ position: cameraPos, fov: 48 }} style={{ background: 'transparent' }}>
            <ambientLight intensity={0.4} />
            <pointLight position={[10, 20, 10]} intensity={1.4} />

            <Grid
              args={[80, 80]}
              cellSize={5}
              cellThickness={0.4}
              cellColor="#111827"
              sectionSize={10}
              sectionThickness={1}
              sectionColor="#1e293b"
              fadeDistance={70}
              position={[0, 0, 0]}
            />

            {/* Geofence ring: hard RTL radius from the loaded show's computed envelope */}
            <GeofenceRing radius={geofenceRadiusM} />

            {/* Origin pad */}
            <mesh position={[0, 0.02, 0]}>
              <cylinderGeometry args={[0.7, 0.7, 0.05, 32]} />
              <meshStandardMaterial color="#38bdf8" emissive="#38bdf8" emissiveIntensity={0.4} />
            </mesh>

            {droneEntries.map(({ id, waypoints, color }) => (
              <DronePathLine key={id} waypoints={waypoints} color={color} />
            ))}

            {droneEntries.map(({ id, waypoints, color }) => {
              const w0 = waypoints[0];
              return (
                <Text key={`lbl-${id}`} position={[w0.y, -w0.z + 1.0, w0.x]} fontSize={0.45} color={color} anchorX="center">
                  {`D${id}`}
                </Text>
              );
            })}

            <OrbitControls makeDefault enableDamping dampingFactor={0.06} />
          </Canvas>
        </div>
      )}

      {trajectoryData && (
        <div style={{ display: 'flex', gap: '0.6rem', padding: '0.45rem 0.75rem', borderTop: '1px solid rgba(255,255,255,0.04)', flexWrap: 'wrap', alignItems: 'center' }}>
          {droneEntries.map(({ id, color }) => (
            <div key={id} style={{ display: 'flex', alignItems: 'center', gap: '0.3rem' }}>
              <div style={{ width: 8, height: 8, borderRadius: '50%', background: color, boxShadow: `0 0 6px ${color}` }} />
              <span style={{ fontSize: '0.68rem', color: 'rgba(255,255,255,0.45)' }}>Drone {id}</span>
            </div>
          ))}
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.3rem', marginLeft: 'auto' }}>
            <div style={{ width: 14, height: 2, background: '#FF3D00', opacity: 0.7 }} />
            <span style={{ fontSize: '0.68rem', color: 'rgba(255,255,255,0.28)' }}>{geofenceRadiusM.toFixed(0)}m Geofence</span>
          </div>
        </div>
      )}
    </div>
  );
}
