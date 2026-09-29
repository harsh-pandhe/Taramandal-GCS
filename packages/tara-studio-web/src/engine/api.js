// Backend endpoints the Studio talks to.

// Studio's own choreography backend. Override at build time with VITE_API_BASE.
export const API_BASE = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8001';

// The GCS fleet backend the "Transmit to GCS" button pushes trajectories to.
// Override with VITE_GCS_BASE (defaults to tara.toml's [ports].gcs_backend, 8090).
export const GCS_BASE = import.meta.env.VITE_GCS_BASE || 'http://localhost:8090';

// Mirrors tara_core.layout.MAX_FLEET_SIZE — kept in sync by hand, same as
// geo.js/telemetry.js on the GCS side. Used only to bound the drone-count input.
export const MAX_FLEET_SIZE = 1000;

const postJson = async (url, body) => {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return res;
};

/** Runs the backend's 4D safety validation over a built show. */
export const validateShow = async (choreo) => {
  // /api/validate takes the choreo dict directly (not wrapped) and only returns
  // `metrics` on success.
  const res = await postJson(`${API_BASE}/api/validate`, choreo);
  if (!res.ok) throw new Error(`Validation failed (${res.status})`);
  return res.json();
};

/**
 * Estimates power draw and battery discharge over a built show (issue #54).
 * `battery` is { chemistry, cells_series, capacity_mah, ambient_temp_c }.
 */
export const validateEnergy = async (choreo, battery) => {
  const res = await postJson(`${API_BASE}/api/validate-energy`, { choreo, ...battery });
  if (!res.ok) {
    const details = await res.json().catch(() => ({}));
    throw new Error(typeof details.detail === 'string' ? details.detail : `Endurance check failed (${res.status})`);
  }
  return res.json();
};

/** Generates a single-shape show through the backend's parametric generators. */
export const generateShow = async (payload) => {
  const res = await postJson(`${API_BASE}/api/generate`, payload);
  if (!res.ok) throw new Error('Shape generation failed.');
  return res.json();
};

/** Renders a show to a downloadable file in the requested format. */
export const exportShow = async ({ choreo, format, homeLat, homeLon, ledTrack, rateHz }) => {
  const res = await postJson(`${API_BASE}/api/export`, {
    choreo, format, home_lat: Number(homeLat), home_lon: Number(homeLon),
    // Only sent when a track exists, so existing callers keep their payload.
    ...(ledTrack ? { led_track: ledTrack } : {}),
    ...(rateHz ? { rate_hz: Number(rateHz) } : {}),
  });
  if (!res.ok) throw new Error('File export failed.');
  return res.json();
};

/** Exports as JSON and uploads the result to the GCS fleet backend. */
export const sendShowToGCS = async ({ choreo, homeLat, homeLon }) => {
  const data = await exportShow({ choreo, format: 'json', homeLat, homeLon });

  const blob = new Blob([data.content], { type: 'application/json' });
  const formData = new FormData();
  formData.append('file', blob, data.filename);

  const gcsRes = await fetch(`${GCS_BASE}/api/upload-trajectory`, {
    method: 'POST', body: formData,
  });
  if (!gcsRes.ok) {
    const details = await gcsRes.json().catch(() => ({}));
    throw new Error(details.detail || 'GCS rejected show file.');
  }
  return gcsRes.json().catch(() => ({}));
};

/** Triggers a browser download of an exported file payload. */
export const downloadExport = (data) => {
  const el = document.createElement('a');
  // Binary formats (.tara) arrive base64-encoded; writing the string straight
  // into a Blob would save the base64 text and produce a file no player can
  // read. Decode back to bytes first.
  const body = data.encoding === 'base64'
    ? Uint8Array.from(atob(data.content), (ch) => ch.charCodeAt(0))
    : data.content;
  const file = new Blob([body], { type: data.mime_type });
  el.href = URL.createObjectURL(file);
  el.download = data.filename;
  document.body.appendChild(el);
  el.click();
  document.body.removeChild(el);
  URL.revokeObjectURL(el.href);
};
