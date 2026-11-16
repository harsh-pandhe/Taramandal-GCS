// Client for the AI audit log API (issues #94, #89): tara_studio/audit_api.py.
//
//   POST /api/projects/{id}/audit              append one entry
//   GET  /api/projects/{id}/audit?limit=N      the latest N, newest first
//
// Same shape as createVaultClient (projectVault.js): the base and the fetch are
// injectable. Unlike it, nothing here throws and nothing is retried: a log
// failure must never get in the way of an edit, and a retry could write the same
// decision twice (see `notStored`).

import { API_BASE } from './api.js';

export const DEFAULT_TIMEOUT_MS = 4000;
const DEFAULT_LIST_LIMIT = 20;
const MAX_LIST_LIMIT = 500;     // the backend's own cap; more is a 422

// The answers that mean the backend looked at the entry and stored nothing:
// project_not_found, audit_full, body_too_large, unsupported_media_type, invalid_entry.
const NOT_STORED_STATUSES = new Set([404, 409, 413, 415, 422]);

const failure = (code, status = 0, notStored = false) => ({ ok: false, code, status, notStored });

/**
 * `notStored` on a failed append is true ONLY when the entry is known not to be
 * in the log: a 4xx answer. For a 503, any other 5xx, a network error, a timeout
 * or an answer that cannot be read it is false, because the entry MAY be on disk:
 * the backend writes the line and then fsyncs it, so an fsync that fails (or a
 * connection that drops after the write) answers with an error for an entry that
 * is already there, and the backend has no way to recognise a repeat. So a caller
 * must not send the same decision again after such a failure.
 */
export function createAuditClient({ base = API_BASE, fetchImpl, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  const url = (projectId) => `${base}/api/projects/${encodeURIComponent(projectId)}/audit`;

  // One request, answered as { res, data } or { error: 'network' | 'timeout' }. Never throws.
  const send = async (target, init) => {
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
    try {
      const doFetch = fetchImpl ?? globalThis.fetch;
      const res = await doFetch(target, { ...init, signal: controller.signal });
      // The timer stays armed while the body is read: a stalled body is a timeout too.
      const data = await res.json().catch(() => null);
      if (timedOut) return { error: 'timeout' };   // the abort cut the body short
      return { res, data };
    } catch {
      return { error: timedOut ? 'timeout' : 'network' };
    } finally {
      clearTimeout(timer);
    }
  };

  // The backend's error code, when it sent one. A 422 from FastAPI's own query
  // validation has `detail` as a LIST and no code.
  const codeOf = (data) => (typeof data?.detail?.code === 'string' ? data.detail.code : 'unknown');

  return {
    /** -> { ok: true, entry } | { ok: false, code, status, notStored } */
    async append(projectId, entry) {
      try {
        if (typeof projectId !== 'string' || !projectId) return failure('invalid_project', 0, true);
        const r = await send(url(projectId), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(entry),
        });
        if (r.error) return failure(r.error);
        const { res, data } = r;
        if (res.ok && data?.entry && typeof data.entry === 'object') return { ok: true, entry: data.entry };
        // 2xx with no readable entry: whatever it was, it may have been stored.
        if (res.ok) return failure('unknown', res.status);
        return failure(codeOf(data), res.status, NOT_STORED_STATUSES.has(res.status));
      } catch {
        return failure('unknown');
      }
    },

    /** -> { ok: true, entries, total, full, chainOk, torn, corruptLines } | { ok: false, code, status } */
    async list(projectId, limit = DEFAULT_LIST_LIMIT) {
      try {
        if (typeof projectId !== 'string' || !projectId) return { ok: false, code: 'invalid_project', status: 0 };
        const n = Math.min(MAX_LIST_LIMIT, Math.max(1, Math.trunc(Number(limit)) || DEFAULT_LIST_LIMIT));
        const r = await send(`${url(projectId)}?limit=${n}`, { method: 'GET' });
        if (r.error) return { ok: false, code: r.error, status: 0 };
        const { res, data } = r;
        if (!res.ok) return { ok: false, code: codeOf(data), status: res.status };
        if (!Array.isArray(data?.entries)) return { ok: false, code: 'unknown', status: res.status };
        return {
          ok: true,
          entries: data.entries,
          total: Number.isInteger(data.total) ? data.total : data.entries.length,
          full: data.full === true,
          chainOk: data.chainOk === true,
          torn: data.torn === true,
          corruptLines: Number.isInteger(data.corruptLines) ? data.corruptLines : 0,
        };
      } catch {
        return { ok: false, code: 'unknown', status: 0 };
      }
    },
  };
}
