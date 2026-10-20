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
  if (!res.ok) {
    // e.g. a 409 explaining that the signing key is missing or unreadable, or
    // a 400 refusing the show — `detail` is a plain string for some of these
    // and a {message, errors} object for others (schema violations, the
    // separation gate), so only the former can be used as-is.
    const details = await res.json().catch(() => ({}));
    const detail = typeof details.detail === 'string' ? details.detail : details.detail?.message;
    throw new Error(detail || 'File export failed.');
  }
  return res.json();
};

/**
 * Whether exports get signed, and with which key (issue #49). The backend
 * holds the private key; this only ever carries its id and public half.
 */
export const getSigningStatus = async () => {
  const res = await fetch(`${API_BASE}/api/signing`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
};

// --- GCS operator session (issue #64) ---------------------------------------
//
// The GCS only accepts uploads from a signed-in CHOREOGRAPHER or
// FLIGHT_COMMANDER. The token is kept per tab in sessionStorage, like the GCS
// dashboard's, and dropped on a 401 so the Transmit panel asks again.

const GCS_SESSION_KEY = 'tara_studio_gcs_session';
const GCS_SESSION_EVENT = 'tara:studio-gcs-session';

export const getGcsSession = () => {
  try {
    const s = JSON.parse(sessionStorage.getItem(GCS_SESSION_KEY) || 'null');
    return s && s.token ? s : null;
  } catch {
    return null;
  }
};

const storeGcsSession = (session) => {
  try {
    if (session) sessionStorage.setItem(GCS_SESSION_KEY, JSON.stringify(session));
    else sessionStorage.removeItem(GCS_SESSION_KEY);
  } catch {
    // storage blocked: the session lasts until the next 401
  }
  window.dispatchEvent(new Event(GCS_SESSION_EVENT));
};

export const onGcsSessionChange = (callback) => {
  window.addEventListener(GCS_SESSION_EVENT, callback);
  return () => window.removeEventListener(GCS_SESSION_EVENT, callback);
};

export const gcsLogin = async (operator, passphrase) => {
  const res = await postJson(`${GCS_BASE}/api/auth/login`, { operator, passphrase });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `GCS sign-in failed (HTTP ${res.status}).`);
  storeGcsSession({ token: body.token, operator: body.operator, role: body.role });
  return body;
};

export const gcsLogout = async () => {
  const session = getGcsSession();
  if (session) {
    await fetch(`${GCS_BASE}/api/auth/logout`, {
      method: 'POST', headers: { Authorization: `Bearer ${session.token}` },
    }).catch(() => {});
  }
  storeGcsSession(null);
};

/**
 * Exports as JSON, signed when a signing key is configured (issue #49), and
 * uploads the show with its .sig.json manifest to the GCS. The GCS verifies
 * the signature before it stages anything; the bytes uploaded are exactly the
 * bytes the Studio backend signed.
 *
 * Takes the same LED track and rate as a download (issue #55), so the backend
 * bakes the same upsampled r/g/b/w frames into both: what is transmitted is
 * the file a designer would get from the JSON button.
 */
export const sendShowToGCS = async ({ choreo, homeLat, homeLon, ledTrack, rateHz }) => {
  const data = await exportShow({ choreo, format: 'json', homeLat, homeLon, ledTrack, rateHz });

  const formData = new FormData();
  formData.append('file', new Blob([data.content], { type: 'application/json' }), data.filename);
  if (data.signature) {
    formData.append(
      'signature',
      new Blob([data.signature.content], { type: 'application/json' }),
      data.signature.filename,
    );
  }

  const session = getGcsSession();
  const gcsRes = await fetch(`${GCS_BASE}/api/upload-trajectory`, {
    method: 'POST', body: formData,
    headers: session ? { Authorization: `Bearer ${session.token}` } : {},
  });
  if (!gcsRes.ok) {
    const details = await gcsRes.json().catch(() => ({}));
    const detail = typeof details.detail === 'string' ? details.detail : details.detail?.message;
    if (gcsRes.status === 401) {
      storeGcsSession(null);
      throw new Error('The GCS needs an operator sign-in: use "Sign in to GCS" in this tab, then transmit again.');
    }
    throw new Error(detail || 'GCS rejected show file.');
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
  // The signature travels as its own file beside the show (show.json +
  // show.json.sig.json); `tara sign` and the GCS upload use the same pair.
  if (data.signature) {
    downloadExport({
      filename: data.signature.filename, content: data.signature.content,
      mime_type: 'application/json', encoding: 'utf-8',
    });
  }
};
