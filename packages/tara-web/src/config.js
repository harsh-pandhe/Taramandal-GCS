// Backend endpoints the dashboard talks to. Override at build time with
// VITE_API_BASE / VITE_WS_URL; the defaults follow tara.toml's gcs_backend port.
export const API_BASE = import.meta.env.VITE_API_BASE || `http://${window.location.hostname || 'localhost'}:8090`;
export const WS_URL = import.meta.env.VITE_WS_URL || `ws://${window.location.hostname || 'localhost'}:8090/ws/telemetry`;

// Keep in sync with NightSkyView's MAX_DRONES instance capacity.
export const MAX_NIGHTSKY_DRONES = 1000;
