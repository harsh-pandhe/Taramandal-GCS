import React, { useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { 
  Play, 
  Pause, 
  RotateCcw, 
  AlertTriangle, 
  CheckCircle, 
  Download, 
  Compass, 
  Globe, 
  Activity,
  Repeat,
  FolderOpen,
  Plus,
  Trash,
  Save,
  Edit2,
  Sparkles,
  X
} from 'lucide-react';
import './App.css';

// Studio's own choreography backend. Override at build time with VITE_API_BASE.
const API_BASE = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8001";
// The GCS fleet backend the "Send to GCS" button pushes trajectories to.
// Override with VITE_GCS_BASE (defaults to the GCS backend's default port 8000).
const GCS_BASE = import.meta.env.VITE_GCS_BASE || "http://localhost:8000";

const interpolateWaypoint = (waypoints, t) => {
  if (!waypoints || waypoints.length === 0) return null;
  if (t <= waypoints[0].time) return waypoints[0];
  if (t >= waypoints[waypoints.length - 1].time) return waypoints[waypoints.length - 1];
  
  for (let i = 0; i < waypoints.length - 1; i++) {
    const w1 = waypoints[i];
    const w2 = waypoints[i+1];
    if (w1.time <= t && t <= w2.time) {
      const ratio = (t - w1.time) / (w2.time - w1.time);
      return {
        x: w1.x + ratio * (w2.x - w1.x),
        y: w1.y + ratio * (w2.y - w1.y),
        z: w1.z + ratio * (w2.z - w1.z),
        yaw: w1.yaw + ratio * (w2.yaw - w1.yaw)
      };
    }
  }
  return waypoints[waypoints.length - 1];
};

// ============================================================================
// Formation library + show sequencer
// A drone show is a *sequence of formations* (point clouds) the fleet morphs
// between, not a single parametric shape. Each formation generator returns N
// target positions in the fleet frame at show altitude (NED z negative = up).
// ============================================================================
const SHOW_ALT = 8.0; // metres above ground for formations

const fmtCircle = (n) => {
  const r = Math.max(4, n * 0.6), cy = n;
  return Array.from({ length: n }, (_, i) => {
    const a = (2 * Math.PI * i) / n;
    return { x: 5 + r * Math.cos(a), y: cy + r * Math.sin(a), z: -SHOW_ALT };
  });
};
const fmtGrid = (n) => {
  const side = Math.ceil(Math.sqrt(n)), sp = 2.5;
  return Array.from({ length: n }, (_, i) => ({ x: (i % side) * sp, y: Math.floor(i / side) * sp, z: -SHOW_ALT }));
};
const fmtLine = (n) => {
  const sp = 2.5;
  return Array.from({ length: n }, (_, i) => ({ x: i * sp, y: n, z: -SHOW_ALT }));
};
const fmtHeart = (n) => Array.from({ length: n }, (_, i) => {
  const t = (2 * Math.PI * i) / n;
  const hx = 16 * Math.pow(Math.sin(t), 3);
  const hy = 13 * Math.cos(t) - 5 * Math.cos(2 * t) - 2 * Math.cos(3 * t) - Math.cos(4 * t);
  return { x: 5 + hx * 0.4, y: n + hy * 0.4, z: -SHOW_ALT };
});
const fmtSphere = (n) => {
  const r = Math.max(4, n * 0.5), cy = n, out = [];
  for (let i = 0; i < n; i++) {
    const phi = Math.acos(1 - (2 * (i + 0.5)) / n);
    const theta = Math.PI * (1 + Math.sqrt(5)) * i;
    out.push({ x: 5 + r * Math.sin(phi) * Math.cos(theta), y: cy + r * Math.sin(phi) * Math.sin(theta), z: -SHOW_ALT - r * Math.cos(phi) });
  }
  return out;
};
const fmtVee = (n) => {
  const sp = 2.0;
  return Array.from({ length: n }, (_, i) => {
    const side = i % 2 === 0 ? 1 : -1, k = Math.ceil(i / 2);
    return { x: k * sp, y: n + side * k * sp, z: -SHOW_ALT };
  });
};
const FORMATIONS = { circle: fmtCircle, grid: fmtGrid, line: fmtLine, heart: fmtHeart, sphere: fmtSphere, vee: fmtVee };
const FORMATION_LABELS = { circle: 'Ring', grid: 'Grid', line: 'Line', heart: 'Heart', sphere: 'Sphere', vee: 'V-Formation' };
const buildFormation = (shape, n) => (FORMATIONS[shape] || fmtCircle)(n);

// Stitches scenes into one full trajectory: takeoff → (hold + morph)* → land.
// Produces the standard {drones:[{id,waypoints:[{time,x,y,z,yaw}]}]} schema so
// preview / validation / LED / export all work unchanged.
// `targetIdx` (optional) is a collision-free assignment: targetIdx[s][i] is the
// index in formation s's point cloud that drone i occupies. Default is identity
// (drone i → point i) — the naive mapping that can make morphs cross.
const buildSequencedShow = (scenes, n, rate, targetIdx = null) => {
  if (!scenes.length || n < 1) return { drones: [], _duration: 0 };
  const TAKEOFF = 5.0, LAND = 5.0;
  const r = Math.max(1, rate), dt = 1 / r;
  const ground = Array.from({ length: n }, (_, i) => ({ x: 0, y: 2 * i, z: 0 }));
  const forms = scenes.map((s) => buildFormation(s.shape, n));
  // Per-drone positions at each formation, honouring the assignment.
  const idx = targetIdx || forms.map(() => Array.from({ length: n }, (_, i) => i));
  const dronePos = forms.map((f, s) => Array.from({ length: n }, (_, i) => f[idx[s][i]]));
  const wps = Array.from({ length: n }, () => []);
  let t = 0;
  const round2 = (v) => parseFloat(v.toFixed(2));
  const push = (positions) => {
    for (let i = 0; i < n; i++) {
      const p = positions[i];
      wps[i].push({ time: round2(t), x: round2(p.x), y: round2(p.y), z: round2(p.z), yaw: 0 });
    }
  };
  const lerp = (A, B, k) => A.map((a, i) => ({ x: a.x + (B[i].x - a.x) * k, y: a.y + (B[i].y - a.y) * k, z: a.z + (B[i].z - a.z) * k }));

  // A hover point directly above each ground pad, at show altitude. Drones take
  // off and land purely vertically over their own pad (no horizontal motion → no
  // crossing); all horizontal repositioning is done as layered morphs at altitude.
  const padHover = Array.from({ length: n }, (_, i) => ({ x: 0, y: 2 * i, z: -SHOW_ALT }));

  // Horizontal morph with per-drone transient transit altitude: crossing straight
  // paths pass at different heights (assignment shortens travel but can't stop two
  // segments intersecting). Centred layering balances the excursion; the trapezoid
  // holds full vertical separation through the crossing-prone middle. z is NED
  // (negative = up), so subtracting the lift raises the drone. TRANSIT_GAP > 1.5 m.
  // Vertical layer spacing during a morph. Must clear the STRICTER of the two
  // safety limits in this pipeline: Studio's own validator uses 0.8 m, but the
  // GCS backend's pre-flight collision check (backend/trajectory_parser.py,
  // matching the live 10 Hz proximity monitor) uses 1.5 m — a real mismatch
  // this dress rehearsal caught: a show that passed Studio's check was still
  // rejected at GCS upload. 2.0 m clears 1.5 m with margin.
  const TRANSIT_GAP = 2.0;
  const MAX_H = 3.2, MAX_V = 2.4;      // velocity budget (m/s); ×1.5 smoothstep peak stays < 6
  const smooth = (u) => u * u * (3 - 2 * u);          // ease-in-out: zero velocity at ends
  // Smoothstep-rounded trapezoid for the transit lift: full separation through the
  // crossing-prone middle, eased corners so acceleration stays within limits. A
  // short 0.15 ramp (vs. a wider one) reaches full vertical separation quickly,
  // minimizing time spent at partial lift where horizontal closing distance can
  // combine with incomplete vertical separation to breach the safety limit —
  // exactly what the live dress rehearsal caught at the takeoff→first-formation
  // morph (min separation briefly dipped to ~1.4 m against the 1.5 m GCS limit).
  const TRANSIT_RAMP = 0.15; // fraction of the morph spent ramping the lift up/down
  const transitProfile = (kk) => {
    const raw = Math.max(0, Math.min(1, kk / TRANSIT_RAMP, (1 - kk) / TRANSIT_RAMP));
    return smooth(raw);
  };
  const morph = (from, to, secs) => {
    // Auto-pace: never exceed the velocity budget, regardless of the user's
    // requested transition time (respect the safety limit over timing). Both the
    // horizontal move and the vertical lift ease in/out (smoothstep) so velocity
    // is zero at the hold junctions — no acceleration spikes. The lift covers
    // `maxLift` over just the TRANSIT_RAMP fraction of the morph (not the whole
    // thing), so its effective speed requirement is scaled accordingly.
    const maxTravel = Math.max(...from.map((a, i) => Math.hypot(a.x - to[i].x, a.y - to[i].y)));
    const maxLift = TRANSIT_GAP * (n - 1) / 2;
    const secsSafe = Math.max(secs, maxTravel / MAX_H, maxLift / (TRANSIT_RAMP * MAX_V));
    const steps = Math.max(1, Math.round(secsSafe * r));
    for (let k = 1; k <= steps; k++) {
      const kk = k / steps, prof = transitProfile(kk);
      push(lerp(from, to, smooth(kk)).map((p, i) => ({ x: p.x, y: p.y, z: p.z - TRANSIT_GAP * (i - (n - 1) / 2) * prof })));
      t += dt;
    }
  };
  const straight = (from, to, secs, incFirst) => {
    const steps = Math.max(1, Math.round(secs * r));
    for (let k = incFirst ? 0 : 1; k <= steps; k++) { push(lerp(from, to, k / steps)); t += dt; }
  };

  straight(ground, padHover, TAKEOFF, true);   // vertical take-off from pads
  morph(padHover, dronePos[0], 3.0);            // fly out to first formation
  for (let s = 0; s < dronePos.length; s++) {
    const holdSteps = Math.round((scenes[s].hold || 3) * r);
    for (let k = 0; k <= holdSteps; k++) { push(dronePos[s]); t += dt; }
    if (s < dronePos.length - 1) morph(dronePos[s], dronePos[s + 1], scenes[s].transition || 3);
  }
  morph(dronePos[dronePos.length - 1], padHover, 3.0); // return over pads
  straight(padHover, ground, LAND, false);             // vertical landing

  return { drones: wps.map((w, i) => ({ id: i, waypoints: w })), _duration: round2(t - dt) };
};

// Computes the collision-free assignment chain across a scene list by asking the
// backend for the min-travel matching between each consecutive formation pair.
// Returns targetIdx[s][i] (point index drone i occupies at formation s), or null
// if the backend is unreachable (caller falls back to identity mapping).
const computeAssignmentChain = async (scenes, n, apiBase) => {
  const forms = scenes.map((s) => buildFormation(s.shape, n));
  const targetIdx = [Array.from({ length: n }, (_, i) => i)]; // formation 0: identity
  for (let s = 0; s < forms.length - 1; s++) {
    const cur = targetIdx[s];
    const fromPts = Array.from({ length: n }, (_, i) => forms[s][cur[i]]);
    const resp = await fetch(`${apiBase}/api/assign`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from: fromPts, to: forms[s + 1] }),
    });
    if (!resp.ok) throw new Error(`assign failed (${resp.status})`);
    const data = await resp.json();
    targetIdx.push(data.assignment);
  }
  return targetIdx;
};

export default function App() {
  const containerRef = useRef(null);
  const rendererRef = useRef(null);
  const dronesGroupRef = useRef(null);
  const pathsGroupRef = useRef(null);
  const gridHelperRef = useRef(null);
  const cameraRef = useRef(null);
  const viewModeRef = useRef("3d");
  
  // Dashboard Navigation Page Tab
  const [activePage, setActivePage] = useState("choreo"); // 'choreo', 'projects', 'fleet'
  const [projectName, setProjectName] = useState("Standard Orbit Formation");
  const [activeProjectId, setActiveProjectId] = useState("default-proj");
  const [drawerOpen, setDrawerOpen] = useState(true);
  const [drawerTab, setDrawerTab] = useState("design"); // 'design', 'safety', 'transmit'
  
  // Dynamic swarm fleet list (single source of truth)
  const [dronesList, setDronesList] = useState([
    { id: 0, name: "Drone 00", hoverAlt: 2.0, spawnOffset: 0.0, colorHex: "#38bdf8" },
    { id: 1, name: "Drone 01", hoverAlt: 2.7, spawnOffset: 2.0, colorHex: "#10b981" },
    { id: 2, name: "Drone 02", hoverAlt: 3.4, spawnOffset: 4.0, colorHex: "#f59e0b" }
  ]);
  
  const [objFileText, setObjFileText] = useState("");
  const [objFileName, setObjFileName] = useState("");
  
  const [audioUrl, setAudioUrl] = useState("");
  const [audioVolume, setAudioVolume] = useState(0);
  const audioRef = useRef(null);
  const audioContextRef = useRef(null);
  const analyserRef = useRef(null);
  const dataArrayRef = useRef(null);
  
  const setupAudioAnalyzer = (file) => {
    if (!file) return;
    const url = URL.createObjectURL(file);
    setAudioUrl(url);
    
    try {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      const ctx = new AudioContext();
      audioContextRef.current = ctx;
      
      const audio = new Audio(url);
      audioRef.current = audio;
      
      const source = ctx.createMediaElementSource(audio);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 64;
      analyserRef.current = analyser;
      
      source.connect(analyser);
      analyser.connect(ctx.destination);
      
      const bufferLength = analyser.frequencyBinCount;
      dataArrayRef.current = new Uint8Array(bufferLength);
    } catch (err) {
      console.warn("Audio Context init blocked or failed: ", err);
      audioRef.current = new Audio(url);
    }
  };

  const parseObjFile = (text) => {
    const lines = text.split('\n');
    const vertices = [];
    for (let line of lines) {
      const trimmed = line.trim();
      if (trimmed.startsWith('v ')) {
        const parts = trimmed.split(/\s+/).slice(1).map(Number);
        if (parts.length >= 3 && !parts.some(isNaN)) {
          vertices.push({ x: parts[0], y: parts[1], z: parts[2] });
        }
      }
    }
    return vertices;
  };
  
  const numDrones = dronesList.length;
  
  const setNumDrones = (targetLen) => {
    setDronesList(prev => {
      let updated = [...prev];
      if (updated.length < targetLen) {
        const colors = ["#38bdf8", "#10b981", "#f59e0b", "#ec4899", "#8b5cf6", "#3b82f6", "#f43f5e", "#10b981", "#e11d48", "#2563eb"];
        for (let i = updated.length; i < targetLen; i++) {
          updated.push({
            id: i,
            name: `Drone ${String(i).padStart(2, '0')}`,
            hoverAlt: parseFloat((2.0 + i * 0.7).toFixed(1)),
            spawnOffset: parseFloat((2.0 * i).toFixed(1)),
            colorHex: colors[i % colors.length]
          });
        }
      } else if (updated.length > targetLen) {
        updated = updated.slice(0, targetLen);
      }
      return updated;
    });
  };

  // Projects Vault repository list
  const [projectsList, setProjectsList] = useState(() => {
    const saved = localStorage.getItem("taramandal_projects");
    return saved ? JSON.parse(saved) : [
      {
        id: "default-proj",
        name: "Standard Orbit Formation",
        shape: "circle",
        duration: 30.0,
        rate: 2.0,
        homeLat: 12.9716,
        homeLon: 77.5946,
        dronesList: [
          { id: 0, name: "Drone 00", hoverAlt: 2.0, spawnOffset: 0.0, colorHex: "#38bdf8" },
          { id: 1, name: "Drone 01", hoverAlt: 2.7, spawnOffset: 2.0, colorHex: "#10b981" },
          { id: 2, name: "Drone 02", hoverAlt: 3.4, spawnOffset: 4.0, colorHex: "#f59e0b" }
        ],
        choreoData: null,
        validation: null
      }
    ];
  });
  
  // Save projects to localStorage
  useEffect(() => {
    localStorage.setItem("taramandal_projects", JSON.stringify(projectsList));
  }, [projectsList]);

  // Input settings
  const [shape, setShape] = useState("circle");
  const [duration, setDuration] = useState(30.0);
  const [rate, setRate] = useState(2.0);
  const [prompt, setPrompt] = useState("");
  const [promptHint, setPromptHint] = useState(null);

  // LED / light choreography
  const [lightEffect, setLightEffect] = useState("perDrone"); // perDrone | solid | rainbow | gradient | pulse
  const [lightColor, setLightColor] = useState("#38bdf8");

  // Show sequencer — ordered list of formation scenes
  const [scenes, setScenes] = useState([
    { id: 1, shape: 'circle', hold: 4, transition: 3 },
    { id: 2, shape: 'heart', hold: 4, transition: 3 },
    { id: 3, shape: 'grid', hold: 4, transition: 3 },
  ]);
  
  // Geography Reference
  const [homeLat, setHomeLat] = useState(12.9716);
  const [homeLon, setHomeLon] = useState(77.5946);
  
  // Data state
  const [choreoData, setChoreoData] = useState(null);
  const [validation, setValidation] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [sendingToGCS, setSendingToGCS] = useState(false);
  
  // Security Access Barrier.
  // Opt-in dev bypass: set VITE_DISABLE_AUTH=true to skip the passphrase gate in
  // local/dev/demo builds. Defaults to auth ON, so production builds are unaffected.
  const AUTH_DISABLED = import.meta.env.VITE_DISABLE_AUTH === "true";
  const [passcode, setPasscode] = useState("");
  const [isUnlocked, setIsUnlocked] = useState(() => {
    return AUTH_DISABLED || sessionStorage.getItem("taramandal_unlocked") === "true";
  });
  const [accessError, setAccessError] = useState("");

  const handleUnlock = (e) => {
    e.preventDefault();
    if (passcode.toUpperCase() === "TARAMANDAL_2026") {
      setIsUnlocked(true);
      sessionStorage.setItem("taramandal_unlocked", "true");
      setAccessError("");
    } else {
      setAccessError("ACCESS DENIED: INVALID SYSTEM PASSKEY");
    }
  };
  
  const handleSaveCurrentProject = () => {
    setProjectsList(prev => prev.map(p => {
      if (p.id === activeProjectId) {
        return {
          ...p,
          name: projectName,
          shape: shape,
          duration: duration,
          rate: rate,
          homeLat: homeLat,
          homeLon: homeLon,
          dronesList: dronesList,
          choreoData: choreoData,
          validation: validation
        };
      }
      return p;
    }));
  };

  const handleLoadProject = (projId) => {
    const proj = projectsList.find(p => p.id === projId);
    if (!proj) return;
    
    setActiveProjectId(projId);
    setProjectName(proj.name);
    setShape(proj.shape);
    setDuration(proj.duration);
    setRate(proj.rate);
    setHomeLat(proj.homeLat);
    setHomeLon(proj.homeLon);
    setDronesList(proj.dronesList);
    setChoreoData(proj.choreoData);
    setValidation(proj.validation);
    setCurrentTime(0.0);
    setActivePage("choreo");
  };

  const handleCreateNewProject = () => {
    const newId = `proj-${Date.now()}`;
    const newProj = {
      id: newId,
      name: "New Swarm Choreography",
      shape: "circle",
      duration: 30.0,
      rate: 2.0,
      homeLat: 12.9716,
      homeLon: 77.5946,
      dronesList: [
        { id: 0, name: "Drone 00", hoverAlt: 2.0, spawnOffset: 0.0, colorHex: "#38bdf8" },
        { id: 1, name: "Drone 01", hoverAlt: 2.7, spawnOffset: 2.0, colorHex: "#10b981" },
        { id: 2, name: "Drone 02", hoverAlt: 3.4, spawnOffset: 4.0, colorHex: "#f59e0b" }
      ],
      choreoData: null,
      validation: null
    };
    
    setProjectsList(prev => [...prev, newProj]);
    setActiveProjectId(newId);
    setProjectName(newProj.name);
    setShape(newProj.shape);
    setDuration(newProj.duration);
    setRate(newProj.rate);
    setHomeLat(newProj.homeLat);
    setHomeLon(newProj.homeLon);
    setDronesList(newProj.dronesList);
    setChoreoData(null);
    setValidation(null);
    setCurrentTime(0.0);
    setActivePage("choreo");
  };

  const handleDeleteProject = (projId, e) => {
    e.stopPropagation();
    if (projectsList.length <= 1) {
      alert("Cannot delete the only remaining project.");
      return;
    }
    const remaining = projectsList.filter(p => p.id !== projId);
    setProjectsList(remaining);
    if (activeProjectId === projId) {
      handleLoadProject(remaining[0].id);
    }
  };

  const handleClearArena = () => {
    setChoreoData(null);
    setValidation(null);
    setCurrentTime(0.0);
  };

  const handleAddDrone = () => {
    if (dronesList.length >= 10) return;
    const nextId = dronesList.length > 0 ? Math.max(...dronesList.map(d => d.id)) + 1 : 0;
    const colors = ["#38bdf8", "#10b981", "#f59e0b", "#ec4899", "#8b5cf6", "#3b82f6", "#f43f5e", "#10b981", "#e11d48", "#2563eb"];
    const nextColor = colors[nextId % colors.length];
    
    setDronesList(prev => [
      ...prev,
      {
        id: nextId,
        name: `Drone ${String(nextId).padStart(2, '0')}`,
        hoverAlt: parseFloat((2.0 + nextId * 0.7).toFixed(1)),
        spawnOffset: parseFloat((2.0 * nextId).toFixed(1)),
        colorHex: nextColor
      }
    ]);
  };

  const handleRemoveDrone = (idToRemove) => {
    if (dronesList.length <= 1) return;
    setDronesList(prev => prev.filter(d => d.id !== idToRemove));
  };

  const handleUpdateDrone = (idToUpdate, field, val) => {
    setDronesList(prev => prev.map(d => {
      if (d.id === idToUpdate) {
        return { ...d, [field]: val };
      }
      return d;
    }));
  };
  
  // View mode selection
  const [viewMode, setViewMode] = useState("3d"); // '3d', 'top-down', 'matrix'
  
  // Playback state
  const [currentTime, setCurrentTime] = useState(0.0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [playbackSpeed, setPlaybackSpeed] = useState(1.0);
  const [isLooping, setIsLooping] = useState(true);
  
  // Sync viewModeRef and update camera position when viewMode changes
  useEffect(() => {
    viewModeRef.current = viewMode;
    if (!cameraRef.current) return;
    
    if (viewMode === "top-down") {
      // Look straight down at the flight field
      cameraRef.current.position.set(5, 30, 5);
      cameraRef.current.lookAt(5, 0, 5);
    } else if (viewMode === "3d") {
      // Perspective standard angles
      cameraRef.current.position.set(10, 15, 20);
      cameraRef.current.lookAt(5, 0, 5);
    }
  }, [viewMode]);
  
  // Generate on start
  useEffect(() => {
    handleGenerate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  
  // 3D Scene setup
  useEffect(() => {
    if (!containerRef.current) return;
    
    // Scene setup
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x060913);
    
    // Camera
    const camera = new THREE.PerspectiveCamera(
      60, 
      containerRef.current.clientWidth / containerRef.current.clientHeight, 
      0.1, 
      1000
    );
    // Position camera looking down at the flight field
    camera.position.set(10, 15, 20);
    camera.lookAt(5, 0, 5);
    cameraRef.current = camera;
    
    // Renderer
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(containerRef.current.clientWidth, containerRef.current.clientHeight);
    containerRef.current.appendChild(renderer.domElement);
    rendererRef.current = renderer;
    
    // Grid Helper representing ground field
    const gridHelper = new THREE.GridHelper(40, 40, 0x38bdf8, 0x1e293b);
    gridHelper.position.set(5, 0, 5);
    scene.add(gridHelper);
    gridHelperRef.current = gridHelper;
    
    // Lighting
    const ambientLight = new THREE.AmbientLight(0xffffff, 0.4);
    scene.add(ambientLight);
    
    const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
    dirLight.position.set(10, 20, 10);
    scene.add(dirLight);
    
    // Groups for active meshes
    const dronesGroup = new THREE.Group();
    scene.add(dronesGroup);
    dronesGroupRef.current = dronesGroup;
    
    const pathsGroup = new THREE.Group();
    scene.add(pathsGroup);
    pathsGroupRef.current = pathsGroup;
    
    // Draw home origin axes
    const axesHelper = new THREE.AxesHelper(3);
    scene.add(axesHelper);
    
    // Simple drag orbit controls simulated mathematically
    let isDragging = false;
    let prevMouseX = 0;
    let prevMouseY = 0;
    let theta = Math.PI / 4;
    let phi = Math.PI / 3;
    let radius = 25;
    
    const updateCamera = () => {
      camera.position.x = 5 + radius * Math.sin(phi) * Math.sin(theta);
      camera.position.z = 5 + radius * Math.sin(phi) * Math.cos(theta);
      camera.position.y = radius * Math.cos(phi);
      camera.lookAt(5, 1, 5);
    };
    
    updateCamera();
    
    const handleMouseDown = (e) => {
      isDragging = true;
      prevMouseX = e.clientX;
      prevMouseY = e.clientY;
    };
    
    const handleMouseMove = (e) => {
      if (!isDragging || viewModeRef.current === "top-down") return;
      const dx = e.clientX - prevMouseX;
      const dy = e.clientY - prevMouseY;
      
      theta -= dx * 0.005;
      phi -= dy * 0.005;
      phi = Math.max(0.1, Math.min(Math.PI / 2.1, phi)); // keep camera above ground
      
      prevMouseX = e.clientX;
      prevMouseY = e.clientY;
      updateCamera();
    };
    
    const handleMouseUp = () => {
      isDragging = false;
    };
    
    const handleWheel = (e) => {
      if (viewModeRef.current === "top-down") {
        camera.position.y += e.deltaY * 0.02;
        camera.position.y = Math.max(5, Math.min(80, camera.position.y));
        return;
      }
      radius += e.deltaY * 0.02;
      radius = Math.max(5, Math.min(100, radius));
      updateCamera();
    };
    
    const container = containerRef.current;
    container.addEventListener('mousedown', handleMouseDown);
    container.addEventListener('mousemove', handleMouseMove);
    window.addEventListener('mouseup', handleMouseUp);
    container.addEventListener('wheel', handleWheel);
    
    // Resize Handler
    const handleResize = () => {
      if (!containerRef.current) return;
      camera.aspect = containerRef.current.clientWidth / containerRef.current.clientHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(containerRef.current.clientWidth, containerRef.current.clientHeight);
    };
    window.addEventListener('resize', handleResize);
    
    // Render loop
    let animationId;
    const animate = () => {
      animationId = requestAnimationFrame(animate);
      renderer.render(scene, camera);
    };
    animate();
    
    // Cleanup
    return () => {
      cancelAnimationFrame(animationId);
      container.removeEventListener('mousedown', handleMouseDown);
      container.removeEventListener('mousemove', handleMouseMove);
      window.removeEventListener('mouseup', handleMouseUp);
      container.removeEventListener('wheel', handleWheel);
      window.removeEventListener('resize', handleResize);
      // Guard removeChild: the container may already be detached (e.g. after a
      // view switch), which would otherwise throw NotFoundError and blank the app.
      if (renderer.domElement && container && container.contains(renderer.domElement)) {
        container.removeChild(renderer.domElement);
      }
      renderer.dispose();
    };
  }, []);
  
  // Re-draw static path lines whenever choreoData changes
  useEffect(() => {
    if (!pathsGroupRef.current || !choreoData) return;
    
    // Clear old lines
    while (pathsGroupRef.current.children.length > 0) {
      const obj = pathsGroupRef.current.children[0];
      pathsGroupRef.current.remove(obj);
    }
    
    const colors = [0x38bdf8, 0x10b981, 0xf59e0b, 0xec4899, 0x8b5cf6, 0x3b82f6];
    
    choreoData.drones.forEach((drone, idx) => {
      const color = colors[idx % colors.length];
      const points = [];
      
      drone.waypoints.forEach(wp => {
        // NED: convert negative z height back to positive z for Blender representation
        points.push(new THREE.Vector3(wp.x, -wp.z, wp.y));
      });
      
      const geometry = new THREE.BufferGeometry().setFromPoints(points);
      const material = new THREE.LineBasicMaterial({ 
        color: color, 
        linewidth: 2,
        transparent: true,
        opacity: 0.6
      });
      const line = new THREE.Line(geometry, material);
      pathsGroupRef.current.add(line);
    });
  }, [choreoData]);
  
  // Re-create drone models when choreoData or custom drone configs change
  useEffect(() => {
    if (!dronesGroupRef.current || !choreoData) return;
    
    // Clear old objects
    while (dronesGroupRef.current.children.length > 0) {
      dronesGroupRef.current.remove(dronesGroupRef.current.children[0]);
    }
    
    const numDrones = choreoData.drones.length;
    if (numDrones === 0) return;
    
    const colors = [0x38bdf8, 0x10b981, 0xf59e0b, 0xec4899, 0x8b5cf6, 0x3b82f6];
    
    choreoData.drones.forEach((drone, idx) => {
      const droneConfig = dronesList.find(d => d.id === idx);
      const droneColor = droneConfig ? new THREE.Color(droneConfig.colorHex).getHex() : colors[idx % colors.length];
      
      const droneGroup = new THREE.Group();
      droneGroup.name = `drone_${idx}`;
      
      // Main lower utility chassis frame
      const chassisGeo = new THREE.BoxGeometry(0.24, 0.02, 0.24);
      const chassisMat = new THREE.MeshPhongMaterial({ color: 0x1e293b, shininess: 80 });
      const chassis = new THREE.Mesh(chassisGeo, chassisMat);
      droneGroup.add(chassis);
      
      // Central canopy shell containing avionics/battery
      const canopyGeo = new THREE.BoxGeometry(0.14, 0.07, 0.14);
      const canopyMat = new THREE.MeshPhongMaterial({
        color: droneColor,
        shininess: 120,
        // Per-drone self-glow in the drone's own color (dimmed), so each drone
        // reads distinctly even in low-light scenes instead of a flat grey glow.
        emissive: droneColor,
        emissiveIntensity: 0.35
      });
      const canopy = new THREE.Mesh(canopyGeo, canopyMat);
      canopy.name = "canopy";
      canopy.position.set(0, 0.045, 0);
      droneGroup.add(canopy);
      
      // Front optical sensor / camera module (facing Z+)
      const cameraGeo = new THREE.SphereGeometry(0.035, 8, 8);
      const cameraMat = new THREE.MeshPhongMaterial({ color: 0x0f172a, shininess: 100 });
      const camera = new THREE.Mesh(cameraGeo, cameraMat);
      camera.position.set(0, 0.03, 0.08);
      droneGroup.add(camera);
      
      const lensGeo = new THREE.CylinderGeometry(0.015, 0.015, 0.02, 8);
      lensGeo.rotateX(Math.PI / 2);
      const lensMat = new THREE.MeshBasicMaterial({ color: 0x38bdf8 });
      const lens = new THREE.Mesh(lensGeo, lensMat);
      lens.position.set(0, 0.03, 0.098);
      droneGroup.add(lens);
      
      // 4 Carbon-Fiber Tubular Struts (X-configuration)
      const armMat = new THREE.MeshPhongMaterial({ color: 0x334155, shininess: 50 });
      const armPositions = [
        { angle: Math.PI / 4, dx: 0.09, dz: 0.09 },   // Front-Right
        { angle: -Math.PI / 4, dx: -0.09, dz: 0.09 },  // Front-Left
        { angle: 3 * Math.PI / 4, dx: 0.09, dz: -0.09 }, // Back-Right
        { angle: -3 * Math.PI / 4, dx: -0.09, dz: -0.09 } // Back-Left
      ];
      
      armPositions.forEach((armPos) => {
        const armGeo = new THREE.CylinderGeometry(0.012, 0.012, 0.22, 8);
        armGeo.rotateX(Math.PI / 2);
        armGeo.rotateY(armPos.angle);
        const arm = new THREE.Mesh(armGeo, armMat);
        arm.position.set(armPos.dx, 0.01, armPos.dz);
        droneGroup.add(arm);
      });
      
      // 4 Motors & Propellers Group
      const rotorGroup = new THREE.Group();
      rotorGroup.name = "rotors";
      
      const motorMat = new THREE.MeshPhongMaterial({ color: 0x0f172a });
      const propMat = new THREE.MeshBasicMaterial({ color: 0xdddddd, side: THREE.DoubleSide });
      
      const motorPositions = [
        { x: 0.17, z: 0.17, name: "prop_0", isFront: true },   // Front-Right
        { x: -0.17, z: 0.17, name: "prop_1", isFront: true },  // Front-Left
        { x: 0.17, z: -0.17, name: "prop_2", isFront: false }, // Back-Right
        { x: -0.17, z: -0.17, name: "prop_3", isFront: false } // Back-Left
      ];
      
      motorPositions.forEach((motorPos) => {
        // Motor Mount Hub
        const motorGeo = new THREE.CylinderGeometry(0.02, 0.02, 0.04, 8);
        const motor = new THREE.Mesh(motorGeo, motorMat);
        motor.position.set(motorPos.x, 0.03, motorPos.z);
        droneGroup.add(motor);
        
        // Propeller Group Container
        const propContainer = new THREE.Group();
        propContainer.name = motorPos.name;
        propContainer.position.set(motorPos.x, 0.05, motorPos.z);
        
        // Propeller blade 1
        const bladeGeo1 = new THREE.BoxGeometry(0.13, 0.003, 0.016);
        bladeGeo1.translate(0.065, 0, 0); // pivot offset
        const blade1 = new THREE.Mesh(bladeGeo1, propMat);
        propContainer.add(blade1);
        
        // Propeller blade 2 (opposite side)
        const blade2 = new THREE.Mesh(bladeGeo1, propMat);
        blade2.rotation.y = Math.PI;
        propContainer.add(blade2);
        
        // Propeller Center Cap
        const capGeo = new THREE.SphereGeometry(0.015, 8, 8);
        const cap = new THREE.Mesh(capGeo, motorMat);
        propContainer.add(cap);
        
        rotorGroup.add(propContainer);
        
        // LED indicator lights on motors (Aviation standard: RED for front, GREEN for back)
        const ledColor = motorPos.isFront ? 0xef4444 : 0x22c55e;
        const navLedGeo = new THREE.SphereGeometry(0.02, 6, 6);
        const navLedMat = new THREE.MeshBasicMaterial({ color: ledColor });
        const navLed = new THREE.Mesh(navLedGeo, navLedMat);
        navLed.position.set(motorPos.x, 0.01, motorPos.z);
        droneGroup.add(navLed);
      });
      
      droneGroup.add(rotorGroup);
      
      // Bottom route-colored underbelly indicator LED
      const ledGeo = new THREE.SphereGeometry(0.045, 8, 8);
      const ledMat = new THREE.MeshBasicMaterial({ color: droneColor });
      const led = new THREE.Mesh(ledGeo, ledMat);
      led.name = "underbelly_led";
      led.position.set(0, -0.025, 0);
      droneGroup.add(led);
      
      dronesGroupRef.current.add(droneGroup);
    });
  }, [choreoData, dronesList]);
  
  // Update drone coordinates & spin propellers based on currentTime
  useEffect(() => {
    if (!dronesGroupRef.current || !choreoData) return;
    
    choreoData.drones.forEach((drone, idx) => {
      const droneGroup = dronesGroupRef.current.children.find(c => c.name === `drone_${idx}`);
      if (!droneGroup) return;
      
      const wp = interpolateWaypoint(drone.waypoints, currentTime);
      if (wp) {
        // Translate local coordinates to webgl workspace layout (Y up, Z out, etc.)
        droneGroup.position.set(wp.x, -wp.z, wp.y);
        
        // Yaw angle rotation
        const yawRad = -wp.yaw * Math.PI / 180;
        droneGroup.rotation.set(0, yawRad, 0);
        
        // Spin the blades/propellers
        const rotors = droneGroup.getObjectByName("rotors");
        if (rotors) {
          rotors.children.forEach(prop => {
            prop.rotation.y += 0.4;
          });
        }
        
        // Live LED choreography — the drone glows its choreographed colour at
        // this instant. This is what turns the tool from a trajectory previewer
        // into a light-show previewer.
        const ledColor = droneColorAt(idx, currentTime);
        const canopy = droneGroup.children.find(c => c.name === "canopy");
        if (canopy) {
          canopy.material.color.copy(ledColor);
          canopy.material.emissive.copy(ledColor);
          canopy.material.emissiveIntensity = 0.9;
        }

        // Pulse underbelly LED on audio beats, tinted to the choreographed colour
        const led = droneGroup.children.find(c => c.name === "underbelly_led");
        if (led) {
          led.material.color.copy(ledColor);
          const pulseScale = 1.0 + (audioVolume || 0) * 1.8;
          led.scale.set(pulseScale, pulseScale, pulseScale);
        }

        droneGroup.visible = true;
      } else {
        droneGroup.visible = false;
      }
    });
  }, [choreoData, currentTime, audioVolume, lightEffect, lightColor, dronesList]);
  
  // Playback timer loop
  useEffect(() => {
    if (isPlaying) {
      if (audioRef.current) {
        audioRef.current.play().catch(e => console.warn("Audio play blocked by browser autoplay policy:", e));
      }
      if (audioContextRef.current && audioContextRef.current.state === 'suspended') {
        audioContextRef.current.resume();
      }
    } else {
      if (audioRef.current) {
        audioRef.current.pause();
      }
    }

    if (!isPlaying || !choreoData) return;
    
    let lastTime = performance.now();
    let animId;
    
    const tick = () => {
      const now = performance.now();
      const delta = (now - lastTime) / 1000.0;
      lastTime = now;
      
      // Update frequency byte analysis if audio analyser is active
      if (analyserRef.current && dataArrayRef.current) {
        analyserRef.current.getByteFrequencyData(dataArrayRef.current);
        let sum = 0;
        for (let i = 0; i < dataArrayRef.current.length; i++) {
          sum += dataArrayRef.current[i];
        }
        const avg = sum / dataArrayRef.current.length;
        setAudioVolume(avg / 255.0);
      }
      
      setCurrentTime(prev => {
        let next = prev + delta * playbackSpeed;
        if (next >= duration) {
          if (isLooping) {
            next = 0.0; // loop
          } else {
            next = duration;
            setIsPlaying(false);
          }
        }
        
        // Sync audio current time if it drifts beyond threshold
        if (audioRef.current) {
          const diff = Math.abs(audioRef.current.currentTime - next);
          if (diff > 0.2) {
            audioRef.current.currentTime = next;
          }
        }
        
        return next;
      });
      
      animId = requestAnimationFrame(tick);
    };
    
    animId = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(animId);
      if (audioRef.current) {
        audioRef.current.pause();
      }
    };
  }, [isPlaying, playbackSpeed, choreoData, duration, isLooping]);
  
  // AI Prompt Shape Mapper
  // AI Prompt Parameter Parser
  const parsePromptParameters = (text) => {
    const t = text.toLowerCase();
    
    // Parse Shape
    let parsedShape = null;
    if (t.includes("heart")) parsedShape = "heart";
    else if (t.includes("helix") || t.includes("spiral")) parsedShape = "helix";
    else if (t.includes("cube") || t.includes("grid") || t.includes("box")) parsedShape = "cube";
    else if (t.includes("circle") || t.includes("ring") || t.includes("orbit")) parsedShape = "circle";
    else if (t.includes("spline") || t.includes("bezier") || t.includes("curve")) parsedShape = "spline";
    else if (t.includes("mesh") || t.includes("obj") || t.includes("model")) parsedShape = "mesh";
    
    // Parse Drones count (1 to 10)
    let parsedDrones = null;
    const droneMatch = t.match(/\b([1-9]|10)\s*drones?\b/) || t.match(/\bdrone\s*count\s*(?:of\s*)?([1-9]|10)\b/);
    if (droneMatch) {
      parsedDrones = parseInt(droneMatch[1]);
    }
    
    // Parse Duration (10 to 120 seconds)
    let parsedDuration = null;
    const durationMatch = t.match(/\b(\d+)\s*(?:seconds?|secs?|s)\b/) || t.match(/\bduration\s*(?:of\s*)?(\d+)\b/);
    if (durationMatch) {
      const val = parseInt(durationMatch[1]);
      if (val >= 10 && val <= 120) {
        parsedDuration = val;
      }
    }
    
    // Parse Rate (1 to 10 Hz)
    let parsedRate = null;
    const rateMatch = t.match(/\b(\d+(?:\.\d+)?)\s*(?:hz|hertz|fps)\b/) || t.match(/\brate\s*(?:of\s*)?(\d+(?:\.\d+)?)\b/);
    if (rateMatch) {
      const val = parseFloat(rateMatch[1]);
      if (val >= 1.0 && val <= 10.0) {
        parsedRate = val;
      }
    }
    
    return {
      shape: parsedShape,
      numDrones: parsedDrones,
      duration: parsedDuration,
      rate: parsedRate
    };
  };
  
  // Computes a drone's LED colour at time t from the active light effect.
  // Returns a THREE.Color (used by the 3D preview and, via getHexString, export).
  const droneColorAt = (idx, t) => {
    const c = new THREE.Color();
    const n = Math.max(1, numDrones);
    const cfg = dronesList.find(d => d.id === idx);
    const baseHex = cfg ? cfg.colorHex : lightColor;
    switch (lightEffect) {
      case 'solid':
        c.set(lightColor);
        break;
      case 'rainbow': {
        const hue = (((t / (duration || 1)) + idx / n) % 1 + 1) % 1;
        c.setHSL(hue, 0.85, 0.55);
        break;
      }
      case 'gradient': {
        const base = new THREE.Color(lightColor);
        const hsl = {};
        base.getHSL(hsl);
        c.setHSL((hsl.h + (idx / n) * 0.5) % 1, Math.max(0.6, hsl.s), 0.55);
        break;
      }
      case 'pulse': {
        c.set(baseHex);
        const beat = audioVolume ? audioVolume : (0.5 + 0.5 * Math.sin(t * 3));
        c.multiplyScalar(Math.min(1.6, 0.55 + beat));
        break;
      }
      case 'perDrone':
      default:
        c.set(baseHex);
        break;
    }
    return c;
  };

  const handleGenerateFromPrompt = () => {
    if (!prompt.trim()) return;
    const parsed = parsePromptParameters(prompt);
    const recognized = [];
    if (parsed.shape) recognized.push(`shape=${parsed.shape}`);
    if (parsed.numDrones !== null) recognized.push(`drones=${parsed.numDrones}`);
    if (parsed.duration !== null) recognized.push(`duration=${parsed.duration}s`);
    if (parsed.rate !== null) recognized.push(`rate=${parsed.rate}Hz`);

    if (recognized.length === 0) {
      setPromptHint({
        ok: false,
        text: "No parameters recognized. Try keywords: heart / helix / cube / circle / spline, a drone count, duration (s), or rate (Hz).",
      });
      return;
    }
    setPromptHint({ ok: true, text: `Applied → ${recognized.join(" · ")}` });
    handleGenerate();
  };

  // --- Sequencer scene management ---
  const addScene = () => {
    const nextId = scenes.length ? Math.max(...scenes.map(s => s.id)) + 1 : 1;
    setScenes([...scenes, { id: nextId, shape: 'circle', hold: 4, transition: 3 }]);
  };
  const removeScene = (id) => setScenes(scenes.filter(s => s.id !== id));
  const updateScene = (id, patch) => setScenes(scenes.map(s => s.id === id ? { ...s, ...patch } : s));
  const moveScene = (index, dir) => {
    const j = index + dir;
    if (j < 0 || j >= scenes.length) return;
    const copy = [...scenes];
    [copy[index], copy[j]] = [copy[j], copy[index]];
    setScenes(copy);
  };

  const handleBuildSequence = async () => {
    if (!scenes.length) { setError("Add at least one formation scene."); return; }
    setLoading(true);
    setError(null);
    try {
      // Ask the backend for a collision-free drone→target assignment between each
      // consecutive formation, so morphs don't cross. Fall back to identity if
      // the backend is unreachable (still builds, just naive mapping).
      let targetIdx = null;
      try {
        targetIdx = await computeAssignmentChain(scenes, numDrones, API_BASE);
      } catch (e) {
        console.warn("Collision-free assignment unavailable, using naive mapping:", e.message);
      }
      const show = buildSequencedShow(scenes, numDrones, Number(rate), targetIdx);
      setChoreoData(show);
      setDuration(show._duration);
      setCurrentTime(0.0);
      setIsPlaying(false);
      // Validate the stitched show through the backend. /api/validate takes the
      // choreo dict directly (not wrapped), and only returns `metrics` on success.
      fetch(`${API_BASE}/api/validate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(show)
      }).then(r => r.ok ? r.json() : null).then(v => { if (v && v.metrics) setValidation(v); }).catch(() => {});
    } catch (e) {
      setError("Sequence build failed: " + e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleGenerate = async () => {
    setLoading(true);
    setError(null);
    
    let chosenShape = shape;
    let chosenDrones = Number(numDrones);
    let chosenDuration = Number(duration);
    let chosenRate = Number(rate);
    
    if (prompt) {
      const parsed = parsePromptParameters(prompt);
      if (parsed.shape) {
        chosenShape = parsed.shape;
        setShape(parsed.shape);
      }
      if (parsed.numDrones !== null) {
        chosenDrones = parsed.numDrones;
        setNumDrones(parsed.numDrones);
      }
      if (parsed.duration !== null) {
        chosenDuration = parsed.duration;
        setDuration(parsed.duration);
      }
      if (parsed.rate !== null) {
        chosenRate = parsed.rate;
        setRate(parsed.rate);
      }
    }
    
    let spawnOffsets = dronesList.map(d => d.spawnOffset);
    let targetHeights = dronesList.map(d => d.hoverAlt);
    
    if (chosenDrones !== dronesList.length) {
      spawnOffsets = [];
      targetHeights = [];
      for (let i = 0; i < chosenDrones; i++) {
        spawnOffsets.push(2.0 * i);
        targetHeights.push(2.0 + i * 0.7);
      }
    }

    if (chosenShape === "mesh") {
      if (!objFileText) {
        setError("Please upload an OBJ mesh file in the DESIGN panel first.");
        setLoading(false);
        return;
      }
      
      const vertices = parseObjFile(objFileText);
      if (vertices.length === 0) {
        setError("No valid vertices found in OBJ file. Make sure it contains 'v x y z' lines.");
        setLoading(false);
        return;
      }
      
      let minX = Infinity, maxX = -Infinity;
      let minY = Infinity, maxY = -Infinity;
      let minZ = Infinity, maxZ = -Infinity;
      
      vertices.forEach(v => {
        if (v.x < minX) minX = v.x;
        if (v.x > maxX) maxX = v.x;
        if (v.y < minY) minY = v.y;
        if (v.y > maxY) maxY = v.y;
        if (v.z < minZ) minZ = v.z;
        if (v.z > maxZ) maxZ = v.z;
      });
      
      const rangeX = maxX - minX || 1.0;
      const rangeY = maxY - minY || 1.0;
      const rangeZ = maxZ - minZ || 1.0;
      
      // Farthest-point sampling: choose `chosenDrones` vertices that spread as
      // evenly as possible across the mesh, so the swarm actually resembles the
      // shape. (Naive stride sampling clustered drones wherever the OBJ had dense
      // vertices and left sparse regions of the mesh unrepresented.)
      const selectedVertices = [];
      if (vertices.length <= chosenDrones) {
        for (let i = 0; i < chosenDrones; i++) {
          selectedVertices.push(vertices[i % vertices.length]);
        }
      } else {
        const minDist2 = new Array(vertices.length).fill(Infinity);
        let current = 0; // deterministic seed
        const chosenIdx = [0];
        for (let k = 1; k < chosenDrones; k++) {
          const cv = vertices[current];
          let farIdx = 0;
          let farDist = -1;
          for (let i = 0; i < vertices.length; i++) {
            const dx = vertices[i].x - cv.x;
            const dy = vertices[i].y - cv.y;
            const dz = vertices[i].z - cv.z;
            const d = dx * dx + dy * dy + dz * dz;
            if (d < minDist2[i]) minDist2[i] = d;
            if (minDist2[i] > farDist) {
              farDist = minDist2[i];
              farIdx = i;
            }
          }
          chosenIdx.push(farIdx);
          current = farIdx;
        }
        chosenIdx.forEach(i => selectedVertices.push(vertices[i]));
      }
      
      const totalSteps = Math.floor(chosenDuration * chosenRate);
      const droneWps = {};
      
      for (let d_id = 0; d_id < chosenDrones; d_id++) {
        droneWps[d_id] = [];
        const startY = spawnOffsets[d_id] !== undefined ? spawnOffsets[d_id] : (2.0 * d_id);
        const hoverAlt = targetHeights[d_id] !== undefined ? targetHeights[d_id] : (2.0 + d_id * 0.7);
        
        const rawVert = selectedVertices[d_id];
        const normX = ((rawVert.x - minX) / rangeX) * 8.0 - 4.0;
        const normY = ((rawVert.y - minY) / rangeY) * 6.0 + 2.0;
        const normZ = -(((rawVert.z - minZ) / rangeZ) * 4.0 + 2.0);
        
        for (let stepIdx = 0; stepIdx <= totalSteps; stepIdx++) {
          const t = stepIdx / chosenRate;
          let x = 0.0, y = startY, z = 0.0;
          
          if (t <= 5.0) {
            const ratio = t / 5.0;
            z = -(hoverAlt * ratio);
          } else if (t <= 8.0) {
            const ratio = (t - 5.0) / 3.0;
            const smoothRatio = ratio * ratio * (3 - 2 * ratio);
            x = smoothRatio * normX;
            y = startY + smoothRatio * (normY - startY);
            z = -hoverAlt + smoothRatio * (normZ - (-hoverAlt));
          } else if (t <= 22.0) {
            x = normX;
            y = normY;
            z = normZ;
          } else if (t <= 25.0) {
            const ratio = (t - 22.0) / 3.0;
            const smoothRatio = ratio * ratio * (3 - 2 * ratio);
            x = normX + smoothRatio * (0.0 - normX);
            y = normY + smoothRatio * (startY - normY);
            z = normZ + smoothRatio * (-hoverAlt - normZ);
          } else {
            const landDur = chosenDuration - 25.0;
            const ratio = (t - 25.0) / landDur;
            z = -(hoverAlt * (1.0 - ratio));
          }
          
          droneWps[d_id].push({
            time: parseFloat(t.toFixed(2)),
            x: parseFloat(x.toFixed(2)),
            y: parseFloat(y.toFixed(2)),
            z: parseFloat(z.toFixed(2)),
            yaw: 0.0
          });
        }
      }
      
      const generatedChoreo = {
        drones: Object.keys(droneWps).map(idStr => ({
          id: Number(idStr),
          waypoints: droneWps[idStr]
        }))
      };
      
      try {
        const valRes = await fetch(`${API_BASE}/api/validate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(generatedChoreo)
        });
        if (!valRes.ok) throw new Error("Choreography validation check failed.");
        const valData = await valRes.json();
        
        setChoreoData(generatedChoreo);
        setValidation(valData);
        setCurrentTime(0.0);
      } catch (validationErr) {
        setError(validationErr.message);
      } finally {
        setLoading(false);
      }
      return;
    }

    try {
      const res = await fetch(`${API_BASE}/api/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          shape: chosenShape,
          num_drones: chosenDrones,
          duration: chosenDuration,
          rate: chosenRate,
          spawn_offsets: spawnOffsets,
          target_heights: targetHeights
        })
      });
      if (!res.ok) throw new Error("Shape generation failed.");
      
      const data = await res.json();
      setChoreoData(data.choreo);
      setValidation(data.validation);
      setCurrentTime(0.0);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };
  
  // Bakes the live LED colour into every waypoint (r,g,b 0-255) so exported
  // shows carry the light choreography, not just position.
  const buildColoredChoreo = () => {
    if (!choreoData) return choreoData;
    return {
      ...choreoData,
      drones: choreoData.drones.map((d, i) => ({
        ...d,
        waypoints: d.waypoints.map(wp => {
          const c = droneColorAt(i, wp.time);
          return { ...wp, r: Math.round(c.r * 255), g: Math.round(c.g * 255), b: Math.round(c.b * 255) };
        })
      }))
    };
  };

  const handleExport = async (format) => {
    if (!choreoData) return;
    try {
      const res = await fetch(`${API_BASE}/api/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          choreo: buildColoredChoreo(),
          format: format,
          home_lat: Number(homeLat),
          home_lon: Number(homeLon)
        })
      });
      if (!res.ok) throw new Error("File export failed.");
      const data = await res.json();
      
      // Trigger browser download
      const element = document.createElement("a");
      const file = new Blob([data.content], { type: data.mime_type });
      element.href = URL.createObjectURL(file);
      element.download = data.filename;
      document.body.appendChild(element);
      element.click();
      document.body.removeChild(element);
    } catch (e) {
      alert("Error exporting choreography: " + e.message);
    }
  };

  const handleSendToGCS = async () => {
    if (!choreoData) return;
    setSendingToGCS(true);
    try {
      // 1. Export in JSON format (with baked LED colours)
      const res = await fetch(`${API_BASE}/api/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          choreo: buildColoredChoreo(),
          format: "json",
          home_lat: Number(homeLat),
          home_lon: Number(homeLon)
        })
      });
      if (!res.ok) throw new Error("Failed to format choreography for GCS.");
      const data = await res.json();
      
      // 2. Package into FormData
      const blob = new Blob([data.content], { type: "application/json" });
      const formData = new FormData();
      formData.append("file", blob, data.filename);
      
      // 3. Post to GCS backend API (configurable via VITE_GCS_BASE)
      const gcsRes = await fetch(`${GCS_BASE}/api/upload-trajectory`, {
        method: "POST",
        body: formData
      });
      if (!gcsRes.ok) {
        const errorDetails = await gcsRes.json();
        throw new Error(errorDetails.detail || "GCS rejected show file.");
      }
      
      alert("✅ Trajectory successfully pushed to GCS fleet! Run launch checks on GCS dashboard.");
    } catch (e) {
      alert("❌ Failed pushing trajectory to GCS: " + e.message);
    } finally {
      setSendingToGCS(false);
    }
  };
  
  if (!isUnlocked) {
    return (
      <div className="security-gate-wrapper">
        <div className="security-gate-card">
          <svg className="gate-logo" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg" style={{ width: '64px', height: '64px', margin: '0 auto 1.5rem auto', display: 'block', filter: 'drop-shadow(0 0 12px var(--primary-glow))' }}>
            <ellipse cx="32" cy="32" rx="26" ry="10" stroke="var(--primary)" strokeWidth="1.5" transform="rotate(-30 32 32)" strokeDasharray="3 3" />
            <ellipse cx="32" cy="32" rx="26" ry="10" stroke="var(--primary)" strokeWidth="1.5" transform="rotate(30 32 32)" strokeDasharray="3 3" />
            <circle cx="32" cy="32" r="7" fill="#0d1426" stroke="#818cf8" strokeWidth="2" />
            <circle cx="15" cy="22" r="3" fill="var(--primary)" />
            <circle cx="49" cy="42" r="3" fill="var(--primary)" />
            <circle cx="49" cy="22" r="3" fill="#818cf8" />
            <circle cx="15" cy="42" r="3" fill="#818cf8" />
          </svg>
          <div className="gate-brand">
            <h1>TARAMANDAL AEROSPACE</h1>
            <p>FLIGHT TRAJECTORY CONTROL ENGINE</p>
          </div>
          <form className="gate-form" onSubmit={handleUnlock}>
            <div className="form-group">
              <label>FLEET SECURITY PASSPHRASE</label>
              <input 
                type="password" 
                placeholder="Enter authorization key..."
                value={passcode}
                onChange={(e) => setPasscode(e.target.value)}
                autoFocus
              />
            </div>
            {accessError && <div className="access-error-msg">{accessError}</div>}
            <button className="btn-primary gate-btn" type="submit" style={{ marginTop: '1rem' }}>
              AUTHORIZE CONNECTION
            </button>
          </form>
          <div className="gate-footer">
            SECURED END-TO-END SYSTEM ENCRYPTION // VER 2026.1
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="studio-container">
      {/* Workspace */}
      <div className="studio-workspace">
        {/* Leftmost Sidebar brand & Navigation */}
        <aside className="studio-sidebar">
          <div className="sidebar-brand">
            <div className="brand-icon-wrapper">
              <svg viewBox="0 0 100 100" className="brand-logo-svg" style={{ width: '32px', height: '32px', filter: 'drop-shadow(0 0 6px var(--primary-glow))' }}>
                <circle cx="50" cy="50" r="38" fill="none" stroke="var(--primary)" strokeWidth="2" strokeDasharray="6, 4" />
                <circle cx="50" cy="50" r="24" fill="none" stroke="#10b981" strokeWidth="1.5" strokeDasharray="3, 3" />
                <circle cx="50" cy="50" r="10" fill="none" stroke="var(--primary)" strokeWidth="1.5" />
                <circle cx="50" cy="12" r="4" fill="#38bdf8" />
                <circle cx="50" cy="88" r="4" fill="#ec4899" />
                <circle cx="12" cy="50" r="4" fill="#f59e0b" />
                <circle cx="88" cy="50" r="4" fill="#10b981" />
              </svg>
            </div>
            <div className="brand-text-wrapper">
              <h1 className="brand-name">TARAMANDAL</h1>
              <span className="brand-sub">STUDIO V2.0</span>
            </div>
          </div>

          <nav className="sidebar-nav">
            <button className={`nav-btn ${activePage === 'choreo' ? 'active' : ''}`} onClick={() => setActivePage('choreo')}>
              <Compass size={16} />
              <span>Choreographer</span>
            </button>
            <button className={`nav-btn ${activePage === 'projects' ? 'active' : ''}`} onClick={() => setActivePage('projects')}>
              <FolderOpen size={16} />
              <span>Project Vault</span>
            </button>
            <button className={`nav-btn ${activePage === 'fleet' ? 'active' : ''}`} onClick={() => setActivePage('fleet')}>
              <Activity size={16} />
              <span>Swarm Fleet</span>
            </button>
          </nav>

          <div className="sidebar-project-info">
            <label>Active Project</label>
            <div className="project-name-display">
              <Edit2 size={12} style={{ color: 'var(--primary)' }} />
              <input 
                type="text" 
                value={projectName} 
                onChange={(e) => setProjectName(e.target.value)} 
                onBlur={handleSaveCurrentProject}
                className="project-name-input"
              />
            </div>
            <button className="btn-secondary save-proj-btn" style={{ width: '100%', marginTop: '0.75rem', display: 'flex', gap: '0.25rem', alignItems: 'center', justifyContent: 'center' }} onClick={handleSaveCurrentProject}>
              <Save size={14} /> Save Project
            </button>
          </div>

          <div className="sidebar-footer">
            <span style={{ fontSize: '0.65rem', letterSpacing: '1px' }}>SYSTEM // ONLINE</span>
          </div>
        </aside>

        {/* Dynamic Pages Area */}
        {activePage === 'choreo' && (
          <>
            {/* Sliding Control Drawer */}
            <div className={`control-drawer ${drawerOpen ? 'active' : ''}`}>
              <div className="drawer-header">
                <h3>CONTROL CONSOLE</h3>
                <button className="close-drawer-btn" onClick={() => setDrawerOpen(false)}>
                  <X size={16} />
                </button>
              </div>

              {/* Drawer Tabs */}
              <div className="drawer-tabs">
                <button className={`drawer-tab-btn ${drawerTab === 'design' ? 'active' : ''}`} onClick={() => setDrawerTab('design')}>
                  DESIGN
                </button>
                <button className={`drawer-tab-btn ${drawerTab === 'sequence' ? 'active' : ''}`} onClick={() => setDrawerTab('sequence')}>
                  SEQUENCE
                </button>
                <button className={`drawer-tab-btn ${drawerTab === 'light' ? 'active' : ''}`} onClick={() => setDrawerTab('light')}>
                  LIGHT
                </button>
                <button className={`drawer-tab-btn ${drawerTab === 'safety' ? 'active' : ''}`} onClick={() => setDrawerTab('safety')}>
                  SAFETY
                </button>
                <button className={`drawer-tab-btn ${drawerTab === 'transmit' ? 'active' : ''}`} onClick={() => setDrawerTab('transmit')}>
                  TRANSMIT
                </button>
              </div>

              <div className="drawer-content">
                {drawerTab === 'sequence' && (
                  <div className="glass-panel-inner">
                    <h4 className="section-subtitle">Show Sequence · {scenes.length} formations</h4>
                    <div className="scene-list">
                      {scenes.map((sc, idx) => (
                        <div className="scene-card" key={sc.id}>
                          <div className="scene-card-head">
                            <span className="scene-index">{String(idx + 1).padStart(2, '0')}</span>
                            <select value={sc.shape} onChange={(e) => updateScene(sc.id, { shape: e.target.value })} style={{ flex: 1 }}>
                              {Object.keys(FORMATION_LABELS).map(k => <option key={k} value={k}>{FORMATION_LABELS[k]}</option>)}
                            </select>
                            <div className="scene-reorder">
                              <button onClick={() => moveScene(idx, -1)} disabled={idx === 0} title="Move up">▲</button>
                              <button onClick={() => moveScene(idx, 1)} disabled={idx === scenes.length - 1} title="Move down">▼</button>
                            </div>
                            <button className="scene-del" onClick={() => removeScene(sc.id)} disabled={scenes.length <= 1} title="Delete">
                              <Trash size={13} />
                            </button>
                          </div>
                          <div className="scene-card-times">
                            <label>Hold (s)
                              <input type="number" min="1" max="30" value={sc.hold} onChange={(e) => updateScene(sc.id, { hold: Math.max(1, parseInt(e.target.value) || 1) })} />
                            </label>
                            <label>Morph (s)
                              <input type="number" min="1" max="20" value={sc.transition} onChange={(e) => updateScene(sc.id, { transition: Math.max(1, parseInt(e.target.value) || 1) })} />
                            </label>
                          </div>
                        </div>
                      ))}
                    </div>
                    <button className="btn-secondary" onClick={addScene} style={{ display: 'flex', gap: '0.35rem', alignItems: 'center', justifyContent: 'center' }}>
                      <Plus size={14} /> Add Formation
                    </button>
                    <button className="btn-primary" onClick={handleBuildSequence} disabled={loading} style={{ display: 'flex', gap: '0.4rem', alignItems: 'center', justifyContent: 'center' }}>
                      <Sparkles size={14} /> {loading ? 'BUILDING…' : 'Build Sequenced Show'}
                    </button>
                    <p style={{ fontSize: '0.68rem', color: 'var(--text-dim)', margin: 0, lineHeight: 1.5 }}>
                      The fleet takes off, holds each formation, then morphs to the next. Total show length is computed from your holds + morphs.
                    </p>
                  </div>
                )}
                {drawerTab === 'light' && (
                  <div className="glass-panel-inner">
                    <h4 className="section-subtitle">LED Choreography</h4>
                    <div className="form-group">
                      <label>Light Effect</label>
                      <select value={lightEffect} onChange={(e) => setLightEffect(e.target.value)}>
                        <option value="perDrone">Per-Drone Palette</option>
                        <option value="solid">Solid Colour</option>
                        <option value="gradient">Fleet Gradient</option>
                        <option value="rainbow">Rainbow Sweep</option>
                        <option value="pulse">Beat Pulse (music)</option>
                      </select>
                    </div>
                    <div className="form-group">
                      <label>Base Colour {lightEffect === 'perDrone' && '(uses Swarm Fleet colours)'}</label>
                      <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                        <input
                          type="color"
                          value={lightColor}
                          onChange={(e) => setLightColor(e.target.value)}
                          style={{ width: '48px', height: '34px', padding: '2px', cursor: 'pointer' }}
                          disabled={lightEffect === 'perDrone'}
                        />
                        <input
                          type="text"
                          value={lightColor}
                          onChange={(e) => setLightColor(e.target.value)}
                          style={{ fontFamily: 'var(--mono)' }}
                          disabled={lightEffect === 'perDrone'}
                        />
                      </div>
                    </div>
                    <p style={{ fontSize: '0.68rem', color: 'var(--text-dim)', lineHeight: 1.5, margin: 0 }}>
                      LED colour is live in the 3D preview and follows the timeline. Colours bake into CSV/JSON export as <span style={{ fontFamily: 'var(--mono)', color: 'var(--accent)' }}>r,g,b</span> per waypoint.
                    </p>
                    {choreoData && (
                      <div>
                        <label style={{ fontSize: '0.62rem', color: 'var(--text-faint)', textTransform: 'uppercase', letterSpacing: '0.8px', fontFamily: 'var(--mono)', display: 'block', marginBottom: '0.5rem' }}>
                          Live @ {currentTime.toFixed(1)}s
                        </label>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.4rem' }}>
                          {choreoData.drones.map((d, i) => {
                            const hex = '#' + droneColorAt(i, currentTime).getHexString();
                            return (
                              <div key={`swatch-${i}`} title={`Drone ${String(i).padStart(2, '0')} · ${hex}`}
                                style={{ width: '22px', height: '22px', borderRadius: '4px', background: hex, border: '1px solid var(--border)' }} />
                            );
                          })}
                        </div>
                      </div>
                    )}
                  </div>
                )}
                {drawerTab === 'design' && (
                  <>
                    {/* AI Prompt Generation */}
                    <div className="glass-panel-inner">
                      <h4 className="section-subtitle">AI Prompt Generation</h4>
                      <div className="form-group">
                        <label>AI Prompt Description</label>
                        <textarea
                          rows="2"
                          placeholder="E.g., rotating heart for 6 drones over 40 seconds"
                          value={prompt}
                          onChange={(e) => setPrompt(e.target.value)}
                        />
                        <button
                          className="btn-primary"
                          style={{ marginTop: '0.5rem', display: 'flex', gap: '0.4rem', alignItems: 'center', justifyContent: 'center' }}
                          onClick={handleGenerateFromPrompt}
                          disabled={loading || !prompt.trim()}
                        >
                          <Sparkles size={14} />
                          {loading ? 'BUILDING…' : 'Generate from Prompt'}
                        </button>
                        {promptHint && (
                          <p style={{ marginTop: '0.4rem', fontSize: '0.68rem', color: promptHint.ok ? 'var(--accent)' : 'var(--warning)', fontFamily: 'var(--mono)' }}>
                            {promptHint.text}
                          </p>
                        )}
                      </div>
                      <div className="form-row">
                        <div className="form-group">
                          <label>Pattern Shape</label>
                          <select value={shape} onChange={(e) => setShape(e.target.value)}>
                            <option value="circle">Rotating Circle</option>
                            <option value="helix">3D Helix</option>
                            <option value="heart">Spinning Heart</option>
                            <option value="cube">Cubic Grid</option>
                            <option value="spline">Bezier Spline Path</option>
                            <option value="mesh">Custom 3D Mesh (.obj)</option>
                          </select>
                        </div>
                        <div className="form-group">
                          <label>Drone Count</label>
                          <input
                            type="number"
                            min="1"
                            max="10"
                            value={numDrones}
                            onChange={(e) => {
                              const n = Math.max(1, Math.min(10, parseInt(e.target.value) || 1));
                              setNumDrones(n);
                            }}
                          />
                        </div>
                      </div>
                      
                      {shape === "mesh" && (
                        <div className="form-group" style={{ marginTop: '0.25rem' }}>
                          <label style={{ display: 'flex', justifyContent: 'space-between' }}>
                            <span>Upload OBJ Mesh {objFileName && `(${objFileName})`}</span>
                          </label>
                          <input 
                            type="file" 
                            accept=".obj"
                            style={{ fontSize: '0.75rem', color: '#fff', width: '100%', background: 'rgba(0,0,0,0.2)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '6px', padding: '0.4rem' }}
                            onChange={(e) => {
                              const file = e.target.files[0];
                              if (file) {
                                setObjFileName(file.name);
                                const reader = new FileReader();
                                reader.onload = (evt) => {
                                  setObjFileText(evt.target.result);
                                };
                                reader.readAsText(file);
                              }
                            }}
                          />
                        </div>
                      )}

                      <div className="form-row">
                        <div className="form-group">
                          <label>Duration (s)</label>
                          <input 
                            type="number" 
                            min="10" 
                            max="120" 
                            value={duration}
                            onChange={(e) => setDuration(Math.max(10, e.target.value))}
                          />
                        </div>
                        <div className="form-group">
                          <label>Rate (Hz)</label>
                          <input 
                            type="number" 
                            min="1" 
                            max="10" 
                            step="0.5"
                            value={rate}
                            onChange={(e) => setRate(Math.max(1, e.target.value))}
                          />
                        </div>
                      </div>
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.5rem', marginTop: '0.5rem' }}>
                        <button 
                          className="btn-primary" 
                          onClick={handleGenerate}
                          disabled={loading}
                        >
                          {loading ? "BUILDING..." : "Build Show"}
                        </button>
                        <button 
                          className="btn-secondary" 
                          style={{ border: '1px solid var(--danger)', color: 'var(--danger)' }}
                          onClick={handleClearArena}
                        >
                          Clear Arena
                        </button>
                      </div>

                      {/* Music & Audio synchronization segment */}
                      <h4 className="section-subtitle" style={{ marginTop: '1rem' }}>Music Synchronization</h4>
                      <div className="form-group">
                        <label>Load Soundtrack {audioUrl && "(Loaded)"}</label>
                        <input 
                          type="file" 
                          accept="audio/*"
                          style={{ fontSize: '0.75rem', color: '#fff', width: '100%', background: 'rgba(0,0,0,0.2)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '6px', padding: '0.4rem' }}
                          onChange={(e) => {
                            const file = e.target.files[0];
                            if (file) {
                              setupAudioAnalyzer(file);
                            }
                          }}
                        />
                      </div>
                      
                      {audioUrl && (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem', marginTop: '0.25rem' }}>
                          <span style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>Audio Beat Monitor:</span>
                          <div style={{ width: '100%', height: '8px', background: 'rgba(255,255,255,0.05)', borderRadius: '4px', overflow: 'hidden' }}>
                            <div 
                              style={{ 
                                height: '100%', 
                                width: `${(audioVolume * 100).toFixed(0)}%`, 
                                background: 'var(--primary)',
                                boxShadow: '0 0 10px var(--primary-glow)',
                                transition: 'width 0.05s ease-out'
                              }}
                            />
                          </div>
                          <button 
                            className="btn-secondary" 
                            style={{ padding: '0.25rem 0.5rem', fontSize: '0.7rem', color: 'var(--danger)', border: '1px solid var(--danger)', marginTop: '0.25rem' }} 
                            onClick={() => {
                              if (audioRef.current) audioRef.current.pause();
                              setAudioUrl("");
                              audioRef.current = null;
                              audioContextRef.current = null;
                              analyserRef.current = null;
                              dataArrayRef.current = null;
                              setAudioVolume(0);
                            }}
                          >
                            Unload Soundtrack
                          </button>
                        </div>
                      )}
                    </div>
                  </>
                )}

                {drawerTab === 'safety' && (
                  <>
                    {/* Safety Validation Summary */}
                    {validation ? (
                      <div className="glass-panel-inner">
                        <h4 className="section-subtitle">Safety Audit Results</h4>
                        <div className={`validation-summary ${validation.status.toLowerCase()}`} style={{ marginBottom: '1rem' }}>
                          {validation.status === "PASSED" ? (
                            <>
                              <CheckCircle size={18} />
                              <span>{validation.summary}</span>
                            </>
                          ) : (
                            <>
                              <AlertTriangle size={18} />
                              <span>{validation.summary}</span>
                            </>
                          )}
                        </div>
                        
                        {validation.metrics && (
                        <div className="metric-grid" style={{ marginBottom: '1.25rem' }}>
                          <div className="metric-card">
                            <div className="metric-title">Min Sep</div>
                            <div className={`metric-val ${validation.metrics.min_distance_m >= 0.8 ? 'passed' : 'danger'}`}>
                              {validation.metrics.min_distance_m}m
                            </div>
                          </div>
                          <div className="metric-card">
                            <div className="metric-title">Max Speed</div>
                            <div className={`metric-val ${validation.metrics.max_velocity_m_s <= 4.0 ? 'passed' : 'warning'}`}>
                              {validation.metrics.max_velocity_m_s}m/s
                            </div>
                          </div>
                          <div className="metric-card">
                            <div className="metric-title">Max Accel</div>
                            <div className={`metric-val ${validation.metrics.max_acceleration_m_s2 <= 2.0 ? 'passed' : 'warning'}`}>
                              {validation.metrics.max_acceleration_m_s2}m/s²
                            </div>
                          </div>
                          <div className="metric-card">
                            <div className="metric-title">Yaw Rate</div>
                            <div className={`metric-val ${validation.metrics.max_yaw_rate_deg_s <= 180.0 ? 'passed' : 'warning'}`}>
                              {Math.round(validation.metrics.max_yaw_rate_deg_s)}°/s
                            </div>
                          </div>
                        </div>
                        )}

                        <div style={{ display: 'flex', flexDirection: 'column', maxHeight: '200px' }}>
                          <span className="telemetry-label" style={{ fontSize: '0.7rem', marginBottom: '0.5rem', display: 'block' }}>
                            Safety Violations ({
                              validation.violations.proximity.length + 
                              validation.violations.velocity.length + 
                              validation.violations.acceleration.length
                            })
                          </span>
                          
                          <div className="violations-list" style={{ overflowY: 'auto' }}>
                            {validation.violations.proximity.map((v, i) => (
                              <div key={`p-${i}`} className="violation-item">
                                <span>⚠️ Proximity breach (D{v.drones[0]} & D{v.drones[1]})</span>
                                <span style={{ color: 'var(--danger)', fontWeight: 600 }}>{v.distance}m @ {v.time}s</span>
                              </div>
                            ))}
                            {validation.violations.velocity.map((v, i) => (
                              <div key={`v-${i}`} className="violation-item">
                                <span>Speed warning · Drone {v.drone}</span>
                                <span style={{ color: 'var(--warning)', fontWeight: 600 }}>{v.velocity}m/s @ {v.time}s</span>
                              </div>
                            ))}
                            {validation.violations.acceleration.map((v, i) => (
                              <div key={`a-${i}`} className="violation-item">
                                <span>Accel warning · Drone {v.drone}</span>
                                <span style={{ color: 'var(--warning)', fontWeight: 600 }}>{v.acceleration}m/s² @ {v.time}s</span>
                              </div>
                            ))}
                            {validation.violations.proximity.length === 0 && 
                             validation.violations.velocity.length === 0 && 
                             validation.violations.acceleration.length === 0 && (
                               <div style={{ color: 'var(--text-muted)', fontSize: '0.75rem', textAlign: 'center', padding: '1rem' }}>
                                 Zero failsafe exceptions raised.
                               </div>
                            )}
                          </div>
                        </div>
                      </div>
                    ) : (
                      <div style={{ color: 'var(--text-muted)', fontSize: '0.8rem', textAlign: 'center', padding: '2rem 1rem' }}>
                        <CheckCircle size={24} style={{ opacity: 0.3, marginBottom: '0.5rem' }} />
                        <p>No active flight show trajectories generated. Build a show to run validation diagnostics.</p>
                      </div>
                    )}
                  </>
                )}

                {drawerTab === 'transmit' && (
                  <>
                    {/* Exporters and GCS transmission */}
                    <div className="glass-panel-inner">
                      <h4 className="section-subtitle">Venue Calibration</h4>
                      <div className="form-row">
                        <div className="form-group">
                          <label>Venue Lat</label>
                          <input 
                            type="number" 
                            step="0.0001"
                            value={homeLat}
                            onChange={(e) => setHomeLat(e.target.value)}
                          />
                        </div>
                        <div className="form-group">
                          <label>Venue Lon</label>
                          <input 
                            type="number" 
                            step="0.0001"
                            value={homeLon}
                            onChange={(e) => setHomeLon(e.target.value)}
                          />
                        </div>
                      </div>
                      
                      <h4 className="section-subtitle" style={{ marginTop: '1.25rem' }}>Data Transmit & Export</h4>
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.5rem', marginTop: '0.5rem' }}>
                        <button className="btn-secondary" style={{ display: 'flex', gap: '0.25rem', alignItems: 'center', justifyContent: 'center' }} onClick={() => handleExport("json")}>
                          <Download size={14} /> JSON
                        </button>
                        <button className="btn-secondary" style={{ display: 'flex', gap: '0.25rem', alignItems: 'center', justifyContent: 'center' }} onClick={() => handleExport("csv")}>
                          <Download size={14} /> CSV
                        </button>
                      </div>
                      <button className="btn-primary" style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', justifyContent: 'center', marginTop: '0.5rem', width: '100%' }} onClick={() => handleExport("kml")}>
                        <Globe size={16} /> Export Google Earth KML
                      </button>
                      
                      <button 
                        className="btn-primary" 
                        style={{ 
                          display: 'flex', 
                          gap: '0.5rem', 
                          alignItems: 'center', 
                          justifyContent: 'center', 
                          marginTop: '0.75rem',
                          width: '100%',
                          background: 'linear-gradient(135deg, #06b6d4 0%, #0891b2 100%)',
                          boxShadow: '0 0 10px rgba(6, 182, 212, 0.3)'
                        }} 
                        onClick={handleSendToGCS}
                        disabled={sendingToGCS || !choreoData}
                      >
                        <Activity size={16} /> {sendingToGCS ? "Sending..." : "Transmit to GCS Fleet"}
                      </button>
                    </div>
                  </>
                )}
              </div>
            </div>
            
            {/* 3D Visualizer Viewport */}
            <main className="studio-viewport">
              <div className="view-mode-tabs-container">
                <div className="view-mode-tabs">
                  <button className={`view-tab ${viewMode === '3d' ? 'active' : ''}`} onClick={() => setViewMode('3d')}>
                    <Compass size={14} /> 3D SKY VIEW
                  </button>
                  <button className={`view-tab ${viewMode === 'top-down' ? 'active' : ''}`} onClick={() => setViewMode('top-down')}>
                    <Globe size={14} /> 2D TOP-DOWN VIEW
                  </button>
                  <button className={`view-tab ${viewMode === 'matrix' ? 'active' : ''}`} onClick={() => setViewMode('matrix')}>
                    <Activity size={14} /> LIVE MATRIX DATA
                  </button>
                </div>
                <button className="floating-console-btn" onClick={() => setDrawerOpen(true)}>
                  <Activity size={14} />
                  <span>CONTROL DESK</span>
                </button>
              </div>

              {viewMode === 'matrix' ? (
                <div className="matrix-view-container">
                  <h3 className="matrix-title">TELEMETRY MATRIX MULTIPLEXER</h3>
                  <div className="matrix-grid">
                    {choreoData?.drones?.map((drone, idx) => {
                      const wp = interpolateWaypoint(drone.waypoints || [], currentTime);
                      const prevWp = currentTime > 0.1 ? interpolateWaypoint(drone.waypoints, currentTime - 0.1) : null;
                      let speed = 0.0;
                      if (wp && prevWp) {
                        const dist = Math.sqrt((wp.x - prevWp.x)**2 + (wp.y - prevWp.y)**2 + (wp.z - prevWp.z)**2);
                        speed = dist / 0.1;
                      }
                      
                      const droneConfig = dronesList.find(d => d.id === idx);
                      const droneColor = droneConfig ? droneConfig.colorHex : '#38bdf8';
                      const droneName = droneConfig ? droneConfig.name : `Vehicle ${String(idx).padStart(2, '0')}`;

                      return (
                        <div className="matrix-card" key={`matrix-d-${idx}`} style={{ borderLeft: `4px solid ${droneColor}` }}>
                          <div className="matrix-card-header">
                            <span className="matrix-drone-id">{droneName.toUpperCase()}</span>
                            <span className="matrix-status" style={{ color: droneColor }}>STREAMING</span>
                          </div>
                          <div className="matrix-data-row">
                            <div className="matrix-data-item">
                              <label>LOCAL X</label>
                              <span className="value">{wp ? wp.x.toFixed(3) : '0.000'}m</span>
                            </div>
                            <div className="matrix-data-item">
                              <label>LOCAL Y</label>
                              <span className="value">{wp ? wp.y.toFixed(3) : '0.000'}m</span>
                            </div>
                            <div className="matrix-data-item">
                              <label>LOCAL Z (NED)</label>
                              <span className="value">{wp ? wp.z.toFixed(3) : '0.000'}m</span>
                            </div>
                            <div className="matrix-data-item">
                              <label>YAW ANGLE</label>
                              <span className="value">{wp ? wp.yaw.toFixed(1) : '0.0'}°</span>
                            </div>
                            <div className="matrix-data-item">
                              <label>VELOCITY</label>
                              <span className="value" style={{ color: speed > 6.0 ? 'var(--danger)' : '#10b981' }}>{speed.toFixed(2)} m/s</span>
                            </div>
                            <div className="matrix-data-item">
                              <label>SAMPLE INDEX</label>
                              <span className="value">{wp ? Math.round(currentTime * rate) : 0} / {drone.waypoints.length}</span>
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ) : (
                <div ref={containerRef} className="canvas-container">
                  {/* Visual Legend */}
                  <div className="legend-overlay">
                    {dronesList.map((d) => (
                      <div className="legend-item" key={`leg-${d.id}`}>
                        <span className="legend-color" style={{ background: d.colorHex }}></span>
                        <span>{d.name} path</span>
                      </div>
                    ))}
                  </div>
                  
                  {/* Loading indicators */}
                  {loading && (
                    <div style={{ position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%, -50%)', zIndex: 10, background: 'rgba(6,9,19,0.85)', padding: '1.5rem', borderRadius: '8px', border: '1px solid var(--primary)', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                      <Activity size={16} className="anim-spin" style={{ color: 'var(--primary)' }} />
                      <span>Synchronizing Swarm Trajectories...</span>
                    </div>
                  )}
                  
                  {error && (
                    <div style={{ position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%, -50%)', zIndex: 10, background: 'rgba(239,68,68,0.85)', padding: '1.5rem', borderRadius: '8px', border: '1px solid #ff8888', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                      <AlertTriangle size={16} style={{ color: '#ff8888' }} />
                      <span>Error: {error}</span>
                    </div>
                  )}
                  
                  {!choreoData && !loading && (
                    <div className="empty-arena-overlay">
                      <Compass size={48} style={{ color: 'rgba(56,189,248,0.3)', marginBottom: '1rem' }} />
                      <h3>Arena Canvas Empty</h3>
                      <p>Use the generator panel or AI prompt builder to design flight trajectories.</p>
                    </div>
                  )}
                </div>
              )}
              
              {/* Timeline player panel */}
              {choreoData && (
                <div className="timeline-overlay">
                  <div className="timeline-controls">
                    <button className="play-btn" onClick={() => setIsPlaying(!isPlaying)}>
                      {isPlaying ? <Pause size={18} /> : <Play size={18} />}
                    </button>
                    <button className="play-btn" style={{ background: 'transparent', border: '1px solid var(--border-strong)', color: 'var(--text-dim)', boxShadow: 'none' }} onClick={() => setCurrentTime(0.0)}>
                      <RotateCcw size={18} />
                    </button>
                    <button
                      className="play-btn"
                      style={{
                        background: isLooping ? 'var(--accent-tint)' : 'transparent',
                        border: isLooping ? '1px solid var(--accent)' : '1px solid var(--border-strong)',
                        color: isLooping ? 'var(--accent)' : 'var(--text-dim)',
                        boxShadow: 'none'
                      }}
                      onClick={() => setIsLooping(!isLooping)}
                    >
                      <Repeat size={18} />
                    </button>
                    
                    <div className="slider-container">
                      <span className="telemetry-label">TIMELINE</span>
                      <input
                        type="range"
                        className="timeline-slider"
                        min="0"
                        max={duration}
                        step="0.05"
                        value={currentTime}
                        style={{
                          background: `linear-gradient(to right, var(--accent) 0%, var(--accent) ${duration > 0 ? (currentTime / duration) * 100 : 0}%, var(--bg-input) ${duration > 0 ? (currentTime / duration) * 100 : 0}%, var(--bg-input) 100%)`
                        }}
                        onChange={(e) => {
                          const val = Number(e.target.value);
                          setCurrentTime(val);
                          setIsPlaying(false);
                          if (audioRef.current) {
                            audioRef.current.currentTime = val;
                          }
                        }}
                      />
                      <div className="time-display">
                        {currentTime.toFixed(2)}s / {duration.toFixed(1)}s
                      </div>
                    </div>
                    
                    <div style={{ display: 'flex', gap: '0.25rem' }}>
                      <button 
                        className="btn-secondary" 
                        style={{ padding: '0.3rem 0.5rem', fontSize: '0.75rem', width: 'auto', background: playbackSpeed === 1.0 ? 'rgba(56,189,248,0.1)' : 'transparent' }}
                        onClick={() => setPlaybackSpeed(1.0)}
                      >
                        1x
                      </button>
                      <button 
                        className="btn-secondary" 
                        style={{ padding: '0.3rem 0.5rem', fontSize: '0.75rem', width: 'auto', background: playbackSpeed === 2.0 ? 'rgba(56,189,248,0.1)' : 'transparent' }}
                        onClick={() => setPlaybackSpeed(2.0)}
                      >
                        2x
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </main>
          </>
        )}

        {/* Project Vault Tab */}
        {activePage === 'projects' && (
          <main className="studio-viewport-page">
            <div className="vault-header">
              <div>
                <h2>Project Vault</h2>
                <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginTop: '0.25rem' }}>Save progress, switch workspaces, or load choreographies from local storage cache.</p>
              </div>
              <button className="btn-primary" style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }} onClick={handleCreateNewProject}>
                <Plus size={16} /> Create New Show
              </button>
            </div>
            
            <div className="vault-grid">
              {projectsList.map((proj) => {
                const isActive = proj.id === activeProjectId;
                return (
                  <div className={`vault-card ${isActive ? 'active' : ''}`} key={proj.id} onClick={() => handleLoadProject(proj.id)}>
                    <div className="vault-card-body">
                      <h3 className="vault-proj-name">{proj.name}</h3>
                      <div className="vault-proj-meta">
                        <span>Pattern: <strong>{proj.shape.toUpperCase()}</strong></span>
                        <span>Vehicles: <strong>{proj.dronesList.length}</strong></span>
                        <span>Duration: <strong>{proj.duration}s</strong></span>
                        <span>Rate: <strong>{proj.rate}Hz</strong></span>
                      </div>
                      <div className="vault-proj-status">
                        {proj.choreoData ? (
                          <span className="status-badge valid" style={{ display: 'inline-flex', gap: '0.25rem', alignItems: 'center', background: 'rgba(34,197,94,0.1)', color: '#22c55e', padding: '0.2rem 0.5rem', borderRadius: '4px', fontSize: '0.7rem' }}>
                            <CheckCircle size={12} /> Trajectory Active
                          </span>
                        ) : (
                          <span className="status-badge empty" style={{ display: 'inline-flex', gap: '0.25rem', alignItems: 'center', background: 'rgba(245,158,11,0.1)', color: '#f59e0b', padding: '0.2rem 0.5rem', borderRadius: '4px', fontSize: '0.7rem' }}>
                            <AlertTriangle size={12} /> Empty Canvas
                          </span>
                        )}
                      </div>
                    </div>
                    <div className="vault-card-actions" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '1.25rem', paddingTop: '0.75rem', borderTop: '1px solid rgba(255,255,255,0.05)' }}>
                      <button className="btn-primary" style={{ padding: '0.4rem 0.8rem', fontSize: '0.75rem' }} onClick={() => handleLoadProject(proj.id)}>
                        Load Workspace
                      </button>
                      <button className="btn-secondary" style={{ padding: '0.4rem', border: '1px solid var(--danger)', color: 'var(--danger)', background: 'transparent' }} onClick={(e) => handleDeleteProject(proj.id, e)}>
                        <Trash size={14} />
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          </main>
        )}

        {/* Swarm Fleet Configuration Tab */}
        {activePage === 'fleet' && (
          <main className="studio-viewport-page">
            <div className="fleet-header">
              <div>
                <h2>Swarm Fleet Configuration</h2>
                <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginTop: '0.25rem' }}>Customize vehicle spawn offsets, flight altitude, color mapping, and add or remove vehicles.</p>
              </div>
              <button className="btn-primary" style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }} onClick={handleAddDrone} disabled={dronesList.length >= 10}>
                <Plus size={16} /> Add Vehicle
              </button>
            </div>
            
            <div className="fleet-grid">
              {dronesList.map((drone, idx) => (
                <div className="fleet-card" key={drone.id} style={{ borderLeft: `4px solid ${drone.colorHex}` }}>
                  <div className="fleet-card-header">
                    <span className="drone-id-badge" style={{ color: drone.colorHex }}>VEHICLE {String(idx).padStart(2, '0')}</span>
                    <button className="delete-drone-btn" style={{ background: 'transparent', border: 'none', color: 'var(--danger)', cursor: 'pointer' }} onClick={() => handleRemoveDrone(drone.id)} disabled={dronesList.length <= 1}>
                      <X size={16} />
                    </button>
                  </div>
                  
                  <div className="fleet-form">
                    <div className="form-group">
                      <label>Vehicle Call Sign / Name</label>
                      <input 
                        type="text" 
                        value={drone.name} 
                        onChange={(e) => handleUpdateDrone(drone.id, "name", e.target.value)}
                        placeholder="e.g. Drone 01"
                      />
                    </div>
                    
                    <div className="form-row">
                      <div className="form-group">
                        <label>Spawn Y Offset (m)</label>
                        <input 
                          type="number" 
                          step="0.5" 
                          value={drone.spawnOffset} 
                          onChange={(e) => handleUpdateDrone(drone.id, "spawnOffset", parseFloat(e.target.value) || 0)}
                        />
                      </div>
                      <div className="form-group">
                        <label>Hover Altitude (m)</label>
                        <input 
                          type="number" 
                          step="0.1" 
                          value={drone.hoverAlt} 
                          onChange={(e) => handleUpdateDrone(drone.id, "hoverAlt", parseFloat(e.target.value) || 0)}
                        />
                      </div>
                    </div>
                    
                    <div className="form-group">
                      <label>Neon LED Navigation Color</label>
                      <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', marginTop: '0.25rem' }}>
                        <input 
                          type="color" 
                          value={drone.colorHex} 
                          onChange={(e) => handleUpdateDrone(drone.id, "colorHex", e.target.value)}
                          style={{ width: '40px', height: '30px', padding: 0, border: 'none', background: 'none', cursor: 'pointer' }}
                        />
                        <span style={{ fontFamily: 'monospace', fontSize: '0.8rem', color: 'var(--text-muted)' }}>{drone.colorHex.toUpperCase()}</span>
                      </div>
                    </div>
                  </div>
                </div>
              ))}
              
              {dronesList.length < 10 && (
                <div className="fleet-add-card" onClick={handleAddDrone}>
                  <Plus size={24} style={{ color: 'var(--primary)', marginBottom: '0.5rem' }} />
                  <span>Add swarm vehicle</span>
                  <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginTop: '0.25rem' }}>Max Capacity: 10 Drones</span>
                </div>
              )}
            </div>
          </main>
        )}
      </div>
    </div>
  );
}
