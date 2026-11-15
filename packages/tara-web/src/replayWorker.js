// Fetches and decodes one full-rate replay window off the main thread (issue #284).
// The main thread sends { id, url, token }; this answers { id, ok: true, meta, packed } with the window's
// numbers as transferred typed arrays (see replayPack.js), or { id, ok: false, status, detail }.
import { packFrames, packedBuffers } from './replayPack.js';

self.onmessage = async ({ data: { id, url, token } }) => {
  try {
    const res = await fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = body?.detail;
      self.postMessage({ id, ok: false, status: res.status, detail: detail && typeof detail === 'object' ? detail.message : detail });
      return;
    }
    const { frames, ...meta } = body;
    const packed = packFrames(Array.isArray(frames) ? frames : []);
    self.postMessage({ id, ok: true, meta, packed }, packedBuffers(packed));
  } catch (err) {
    self.postMessage({ id, ok: false, status: 0, detail: String(err?.message || err) });
  }
};
