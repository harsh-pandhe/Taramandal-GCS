import { useRef, useEffect } from 'react';
import * as THREE from 'three';
import { interpolateWaypoint } from '../../engine/sequencer.js';
import { droneColorAt } from '../../engine/lighting.js';

// Preview geometry is a single instanced mesh rather than a Group per drone.
// The old per-drone model (chassis, canopy, 4 arms, 4 props, LEDs — ~15 meshes
// each) meant ~1500 draw calls at 100 drones and a full scene-graph rebuild on
// every regenerate; at the fleet sizes this platform targets that was the
// preview's bottleneck. One InstancedMesh draws the whole fleet in one call and
// only its per-instance matrices and colours change per frame.

const UP_AXIS_SCALE = 1; // NED z is negated when mapped to three's Y

/**
 * Three.js preview of a built show.
 *
 * Owns its own render loop and reads playback time through a ref, so scrubbing
 * and playback repaint without re-running this effect or rebuilding the scene.
 */
export default function StageViewport3D({
  choreoData, currentTime, viewMode, lighting, showPaths,
}) {
  const containerRef = useRef(null);
  const stateRef = useRef({});

  // Live values the render loop reads without needing to be re-created.
  const timeRef = useRef(currentTime);
  const lightingRef = useRef(lighting);
  const viewModeRef = useRef(viewMode);
  timeRef.current = currentTime;
  lightingRef.current = lighting;
  viewModeRef.current = viewMode;

  // ---- one-time scene setup -------------------------------------------------
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x060913);

    const camera = new THREE.PerspectiveCamera(
      60, container.clientWidth / container.clientHeight, 0.1, 2000,
    );
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(container.clientWidth, container.clientHeight);
    container.appendChild(renderer.domElement);

    const grid = new THREE.GridHelper(120, 60, 0x38bdf8, 0x1e293b);
    scene.add(grid);
    scene.add(new THREE.AmbientLight(0xffffff, 0.5));
    const dir = new THREE.DirectionalLight(0xffffff, 0.8);
    dir.position.set(10, 20, 10);
    scene.add(dir);
    scene.add(new THREE.AxesHelper(3));

    const pathsGroup = new THREE.Group();
    scene.add(pathsGroup);

    // Orbit state, driven by pointer maths rather than a controls dependency.
    const orbit = { theta: Math.PI / 4, phi: Math.PI / 3, radius: 40, target: new THREE.Vector3(5, 4, 5) };
    const applyCamera = () => {
      if (viewModeRef.current === 'top-down') {
        camera.position.set(orbit.target.x, orbit.radius, orbit.target.z + 0.001);
      } else {
        camera.position.set(
          orbit.target.x + orbit.radius * Math.sin(orbit.phi) * Math.sin(orbit.theta),
          orbit.radius * Math.cos(orbit.phi),
          orbit.target.z + orbit.radius * Math.sin(orbit.phi) * Math.cos(orbit.theta),
        );
      }
      camera.lookAt(orbit.target);
    };
    applyCamera();

    let dragging = false, px = 0, py = 0;
    const onDown = (e) => { dragging = true; px = e.clientX; py = e.clientY; };
    const onMove = (e) => {
      if (!dragging || viewModeRef.current === 'top-down') return;
      orbit.theta -= (e.clientX - px) * 0.005;
      orbit.phi -= (e.clientY - py) * 0.005;
      orbit.phi = Math.max(0.05, Math.min(Math.PI / 2.05, orbit.phi)); // stay above ground
      px = e.clientX; py = e.clientY;
      applyCamera();
    };
    const onUp = () => { dragging = false; };
    const onWheel = (e) => {
      e.preventDefault();
      orbit.radius = Math.max(4, Math.min(400, orbit.radius + e.deltaY * 0.05));
      applyCamera();
    };

    container.addEventListener('pointerdown', onDown);
    container.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    container.addEventListener('wheel', onWheel, { passive: false });

    const onResize = () => {
      if (!container.clientWidth) return;
      camera.aspect = container.clientWidth / container.clientHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(container.clientWidth, container.clientHeight);
    };
    window.addEventListener('resize', onResize);
    // The drawer collapsing changes the canvas size without a window resize.
    const ro = new ResizeObserver(onResize);
    ro.observe(container);

    stateRef.current = { scene, camera, renderer, pathsGroup, orbit, applyCamera, mesh: null };

    const dummy = new THREE.Object3D();
    let frame;
    const animate = () => {
      frame = requestAnimationFrame(animate);
      const st = stateRef.current;
      const { mesh, show } = st;

      if (mesh && show) {
        const t = timeRef.current;
        for (let i = 0; i < show.drones.length; i++) {
          const wp = interpolateWaypoint(show.drones[i].waypoints, t);
          if (!wp) continue;
          // NED (x north, y east, z down) → three (x, y up, z)
          dummy.position.set(wp.x, -wp.z * UP_AXIS_SCALE, wp.y);
          dummy.rotation.set(0, -wp.yaw * Math.PI / 180, 0);
          dummy.updateMatrix();
          mesh.setMatrixAt(i, dummy.matrix);
          const c = droneColorAt(i, t, lightingRef.current);
          mesh.setColorAt(i, c);
        }
        mesh.instanceMatrix.needsUpdate = true;
        if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
      }
      renderer.render(scene, camera);
    };
    animate();

    return () => {
      cancelAnimationFrame(frame);
      container.removeEventListener('pointerdown', onDown);
      container.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      container.removeEventListener('wheel', onWheel);
      window.removeEventListener('resize', onResize);
      ro.disconnect();
      // The container may already be detached (e.g. after a view switch), which
      // would otherwise throw NotFoundError and blank the app.
      if (renderer.domElement.parentNode === container) {
        container.removeChild(renderer.domElement);
      }
      renderer.dispose();
    };
  }, []);

  // ---- rebuild fleet instances when the show changes -------------------------
  useEffect(() => {
    const st = stateRef.current;
    if (!st.scene) return;

    if (st.mesh) {
      st.scene.remove(st.mesh);
      st.mesh.geometry.dispose();
      st.mesh.material.dispose();
      st.mesh = null;
    }
    st.show = choreoData;
    if (!choreoData || !choreoData.drones.length) return;

    const n = choreoData.drones.length;
    // A low-poly body keeps 1000 instances cheap; the LED colour, not the
    // silhouette, is what reads at show distance.
    const geo = new THREE.SphereGeometry(0.28, 10, 8);
    const mat = new THREE.MeshStandardMaterial({
      emissiveIntensity: 1.4, roughness: 0.35, metalness: 0.1,
    });
    // Per-instance colour drives emissive too, so drones glow their own colour.
    // vColor is declared vec3 or vec4 depending on which colour varyings three
    // enables for the material, and totalEmissiveRadiance is a vec3 — so wrap
    // the swizzle in a vec3() constructor, which compiles against either.
    mat.onBeforeCompile = (shader) => {
      shader.fragmentShader = shader.fragmentShader.replace(
        '#include <emissivemap_fragment>',
        '#include <emissivemap_fragment>\n\ttotalEmissiveRadiance = vec3( vColor ) * 1.2;',
      );
    };
    const mesh = new THREE.InstancedMesh(geo, mat, n);
    mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    mesh.frustumCulled = false; // instances move well outside the initial bounds
    st.scene.add(mesh);
    st.mesh = mesh;

    // Frame the show: centre the orbit on the fleet and back off far enough to
    // see it, so large formations aren't off-screen on first build.
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity, maxUp = 0;
    for (const d of choreoData.drones) {
      for (const wp of d.waypoints) {
        if (wp.x < minX) minX = wp.x;
        if (wp.x > maxX) maxX = wp.x;
        if (wp.y < minY) minY = wp.y;
        if (wp.y > maxY) maxY = wp.y;
        if (-wp.z > maxUp) maxUp = -wp.z;
      }
    }
    st.orbit.target.set((minX + maxX) / 2, maxUp / 2, (minY + maxY) / 2);
    st.orbit.radius = Math.max(12, Math.hypot(maxX - minX, maxY - minY) * 1.1 + maxUp);
    st.applyCamera();
  }, [choreoData]);

  // ---- static path lines ----------------------------------------------------
  useEffect(() => {
    const st = stateRef.current;
    if (!st.pathsGroup) return;

    for (const child of [...st.pathsGroup.children]) {
      st.pathsGroup.remove(child);
      child.geometry.dispose();
      child.material.dispose();
    }
    if (!showPaths || !choreoData) return;

    // One merged line set: a Line per drone would put the draw-call cost back
    // that instancing just removed.
    const positions = [];
    for (const drone of choreoData.drones) {
      const wps = drone.waypoints;
      for (let i = 0; i < wps.length - 1; i++) {
        positions.push(wps[i].x, -wps[i].z, wps[i].y);
        positions.push(wps[i + 1].x, -wps[i + 1].z, wps[i + 1].y);
      }
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    const line = new THREE.LineSegments(
      geo,
      new THREE.LineBasicMaterial({ color: 0x38bdf8, transparent: true, opacity: 0.22 }),
    );
    st.pathsGroup.add(line);
  }, [choreoData, showPaths]);

  // ---- view mode ------------------------------------------------------------
  useEffect(() => {
    const st = stateRef.current;
    if (st.applyCamera) st.applyCamera();
  }, [viewMode]);

  return <div ref={containerRef} className="canvas-container" />;
}
