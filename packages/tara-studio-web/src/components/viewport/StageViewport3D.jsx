import { useRef, useEffect, useImperativeHandle } from 'react';
import * as THREE from 'three';
import { interpolateWaypoint } from '../../engine/sequencer.js';
import { droneColorAt } from '../../engine/lighting.js';
import {
  createCamera, cameraPosition, cameraDistance, worldPerPixel,
  orbitBy, zoomBy, panBy, frameBounds, showBounds, fleetBoundsAt,
} from '../../engine/camera.js';
import { cameraShortcut } from '../../engine/shortcuts.js';

// Preview geometry is a single instanced mesh rather than a Group per drone.
// The old per-drone model (chassis, canopy, 4 arms, 4 props, LEDs — ~15 meshes
// each) meant ~1500 draw calls at 100 drones and a full scene-graph rebuild on
// every regenerate; at the fleet sizes this platform targets that was the
// preview's bottleneck. One InstancedMesh draws the whole fleet in one call and
// only its per-instance matrices and colours change per frame.

const UP_AXIS_SCALE = 1; // NED z is negated when mapped to three's Y

// The camera backs off to frame the whole formation (see frameBounds),
// which at large fleet sizes spread over a wide formation can put it hundreds
// of units away. A fixed-radius sphere shrinks with distance like anything
// else in perspective — at N=100 on a wide glyph formation it measured out to
// ~1.3px on screen, effectively invisible, while the path lines stayed
// visible because line width doesn't attenuate with distance the way a mesh's
// apparent size does. BASE_SPHERE_RADIUS is the original, artistically-tuned
// size for close-up formations; MIN_SCALE keeps that unchanged there, and the
// per-frame scale-up below only kicks in once the formation is far enough
// that the fixed size would start disappearing. MAX_SCALE is capped so drones
// stay individually legible rather than merging into a blob at the 2m design
// separation between neighbours.
const BASE_SPHERE_RADIUS = 0.28;
const TARGET_SCREEN_PX = 5;
const MIN_SCALE = 1;
const MAX_SCALE = 3;

/**
 * Three.js preview of a built show.
 *
 * Owns its own render loop and reads playback time through a ref, so scrubbing
 * and playback repaint without re-running this effect or rebuilding the scene.
 *
 * Camera (maths in engine/camera.js):
 *   3D         left drag rotates; right, middle or Shift+left drag pans
 *   top-down   any drag pans
 *   audience   any drag walks along the audience line and tilts the gaze
 *   all views  wheel zooms, F fits the fleet at the current time, Home resets
 *
 * The show is framed automatically until the designer moves the camera; after
 * that a rebuild leaves the view alone. Reset, or a change of `frameKey` (the
 * open project), hands framing back. `ref` exposes { fit, reset }.
 *
 * This component unmounts whenever the designer leaves the stage (another
 * page, the telemetry view), so the camera is kept in `cameraStore`, a ref the
 * parent owns, and picked up again on the way back.
 */
export default function StageViewport3D({
  choreoData, currentTime, viewMode, lighting, showPaths, frameKey, cameraStore, ref,
}) {
  const containerRef = useRef(null);
  const stateRef = useRef({});
  const ownStore = useRef(null);
  const store = cameraStore ?? ownStore;
  if (!store.current) store.current = { cam: createCamera(), userMoved: false, frameKey };

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

    // The camera is driven by pointer maths rather than a controls dependency.
    const lens = () => ({
      fovDeg: camera.fov,
      aspect: container.clientWidth / (container.clientHeight || 1),
    });
    const applyCamera = () => {
      const { cam } = store.current;
      const eye = cameraPosition(cam, viewModeRef.current);
      camera.position.set(eye.x, eye.y, eye.z);
      camera.lookAt(cam.target.x, cam.target.y, cam.target.z);
      // The canvas is WebGL, so this is the only way the e2e suite can see
      // where the camera is.
      container.dataset.camera = JSON.stringify({
        eye: [eye.x, eye.y, eye.z].map((v) => +v.toFixed(2)),
        target: [cam.target.x, cam.target.y, cam.target.z].map((v) => +v.toFixed(2)),
      });
    };
    // A camera change made by the designer, as opposed to automatic framing.
    const moveCamera = (cam) => {
      store.current.cam = cam;
      store.current.userMoved = true;
      applyCamera();
    };
    const fit = () => {
      const bounds = fleetBoundsAt(stateRef.current.show, timeRef.current);
      if (bounds) moveCamera(frameBounds(store.current.cam, bounds, lens()));
    };
    const reset = () => {
      store.current.cam = frameBounds(createCamera(), showBounds(stateRef.current.show), lens());
      store.current.userMoved = false;
      applyCamera();
    };

    let drag = null, px = 0, py = 0;
    const onDown = (e) => {
      const pan = viewModeRef.current !== '3d' || e.button !== 0 || e.shiftKey;
      drag = pan ? 'pan' : 'orbit';
      px = e.clientX; py = e.clientY;
      if (e.button === 1) e.preventDefault(); // no middle-click autoscroll
      container.setPointerCapture?.(e.pointerId);
    };
    const onMove = (e) => {
      if (!drag) return;
      const dx = e.clientX - px, dy = e.clientY - py;
      px = e.clientX; py = e.clientY;
      if (!dx && !dy) return;
      const { cam } = store.current;
      if (drag === 'orbit') {
        moveCamera(orbitBy(cam, dx, dy));
        return;
      }
      const viewMode = viewModeRef.current;
      const metresPerPx = worldPerPixel(
        cameraDistance(cam, viewMode), camera.fov, container.clientHeight,
      );
      moveCamera(panBy(cam, viewMode, dx, dy, metresPerPx));
    };
    const onUp = () => { drag = null; };
    const onWheel = (e) => {
      e.preventDefault();
      moveCamera(zoomBy(store.current.cam, e.deltaY));
    };
    const onContextMenu = (e) => e.preventDefault(); // right drag pans
    const onKeyDown = (e) => {
      const action = cameraShortcut(e);
      if (!action) return;
      e.preventDefault();
      if (action === 'fit') fit(); else reset();
    };

    container.addEventListener('pointerdown', onDown);
    container.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    container.addEventListener('wheel', onWheel, { passive: false });
    container.addEventListener('contextmenu', onContextMenu);
    window.addEventListener('keydown', onKeyDown);

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

    stateRef.current = {
      scene, camera, renderer, pathsGroup, mesh: null, applyCamera, fit, reset, lens,
    };
    applyCamera();

    const dummy = new THREE.Object3D();
    let frame;
    const animate = () => {
      frame = requestAnimationFrame(animate);
      const st = stateRef.current;
      const { mesh, show } = st;

      if (mesh && show) {
        const t = timeRef.current;
        // Constant on-screen size regardless of how far the camera sits:
        // world-size needed for TARGET_SCREEN_PX grows linearly with distance
        // (basic perspective), clamped so nearby formations keep their
        // original tuned size and far/spread ones don't balloon into overlap.
        // Measured to the target rather than read off the orbit radius, which
        // is not the viewing distance in the audience view.
        const worldPerPx = worldPerPixel(
          cameraDistance(store.current.cam, viewModeRef.current), camera.fov, renderer.domElement.clientHeight,
        );
        const scale = Math.min(
          Math.max((TARGET_SCREEN_PX * worldPerPx) / (2 * BASE_SPHERE_RADIUS), MIN_SCALE),
          MAX_SCALE,
        );
        dummy.scale.setScalar(scale);
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
      container.removeEventListener('contextmenu', onContextMenu);
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('resize', onResize);
      ro.disconnect();
      // The container may already be detached (e.g. after a view switch), which
      // would otherwise throw NotFoundError and blank the app.
      if (renderer.domElement.parentNode === container) {
        container.removeChild(renderer.domElement);
      }
      renderer.dispose();
    };
  }, [store]);

  useImperativeHandle(ref, () => ({
    fit: () => stateRef.current.fit?.(),
    reset: () => stateRef.current.reset?.(),
  }), []);

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
  }, [choreoData]);

  // ---- frame the show until the designer takes the camera --------------------
  // Runs after the rebuild above, so st.show is already the new show. Framing
  // on every rebuild would snap the view back after each edit, so it stops
  // once the camera has been moved and resumes for a different project.
  useEffect(() => {
    const st = stateRef.current;
    if (!st.scene) return;
    const kept = store.current;
    if (kept.frameKey !== frameKey) {
      kept.frameKey = frameKey;
      kept.userMoved = false;
    }
    if (kept.userMoved) return;
    const bounds = showBounds(choreoData);
    if (!bounds) return;
    kept.cam = frameBounds(kept.cam, bounds, st.lens());
    st.applyCamera();
  }, [choreoData, frameKey, store]);

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
