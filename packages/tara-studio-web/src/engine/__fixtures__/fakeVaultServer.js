// An in-memory stand-in for the backend's Project Vault API (issue #101), for
// the Vitest suite and the Playwright stubs, neither of which runs the Python
// backend.
//
// It is held to the real thing: vault-contract.json is recorded from the live
// FastAPI server by packages/tara-studio/tests/test_project_vault_contract.py,
// and fakeVaultServer.test.js replays the same requests against this file. If
// the API changes and this does not follow, that test fails.

const ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const MAX_VERSIONS = 20;
const MAX_FLEET_SIZE = 1000;
const SHOW_FORMAT_VERSION = 1;

const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const isObjectList = (v) => Array.isArray(v) && v.every(isObject);
const copy = (v) => JSON.parse(JSON.stringify(v));

const validBody = (body) => isObject(body)
  && typeof body.name === 'string' && body.name.length >= 1 && body.name.length <= 200
  && isObject(body.document)
  && isObjectList(body.document.scenes)
  && isObjectList(body.document.dronesList) && body.document.dronesList.length <= MAX_FLEET_SIZE
  && (body.show == null || isObject(body.show))
  && (body.validation == null || isObject(body.validation))
  && (body.versions === undefined || (
    Array.isArray(body.versions) && body.versions.length <= MAX_VERSIONS
    && body.versions.every((v) => isObject(v) && typeof v.id === 'string' && v.id && typeof v.savedAt === 'string' && v.savedAt)
  ))
  && (body.id == null || typeof body.id === 'string');

// The checks of show_v1 a Studio-built show can actually trip.
const conformingShow = (show) => {
  const numbers = (wp) => ['time', 'x', 'y', 'z', 'yaw'].every((k) => typeof wp[k] === 'number' && Number.isFinite(wp[k]));
  const ok = Array.isArray(show.drones) && show.drones.every((d) => isObject(d)
    && Number.isInteger(d.id) && d.id >= 0 && d.id < MAX_FLEET_SIZE
    && Array.isArray(d.waypoints)
    && d.waypoints.every((wp) => isObject(wp) && numbers(wp) && wp.time >= 0 && wp.yaw >= 0 && wp.yaw < 360));
  return ok ? { version: SHOW_FORMAT_VERSION, ...show } : null;
};

const canonical = (v) => {
  if (Array.isArray(v)) return `[${v.map(canonical).join(',')}]`;
  if (isObject(v)) return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canonical(v[k])}`).join(',')}}`;
  return JSON.stringify(v);
};
// Not the backend's SHA-256; it only has to tell different content apart.
const fingerprint = (body) => {
  let h = 0x811c9dc5;
  for (const ch of canonical({ name: body.name, document: body.document, versions: body.versions ?? [] })) {
    h = Math.imul(h ^ ch.charCodeAt(0), 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, '0');
};

const fail = (status, code, extra = {}) => ({ status, body: { detail: { code, ...extra } } });
const INVALID = { status: 422, body: { detail: '<validation>' } };

/**
 * `handle(method, path, body)` answers one request as `{ status, body }`;
 * `fetch` wraps it for code that takes a fetch implementation. `down = true`
 * makes every request fail the way an unreachable backend does.
 */
export function createFakeVaultServer({ now = () => new Date().toISOString() } = {}) {
  const projects = new Map();   // id -> { meta, show, fingerprint }
  let generated = 0;
  const server = { down: false, requests: [] };

  const view = ({ meta, show }, withShow = false) => copy({
    ...meta, has_show: show !== null, ...(withShow ? { show } : {}),
  });
  const saved = (entry, warnings, extra = {}) => ({ project: view(entry), warnings, ...extra });

  const store = (id, body, revision, createdAt, importedFingerprint = null) => {
    const stamp = now();
    const show = body.show == null ? null : conformingShow(body.show);
    const entry = {
      meta: {
        format: 'tara-project',
        format_version: 1,
        id,
        name: body.name,
        revision,
        created_at: createdAt ?? stamp,
        updated_at: stamp,
        document: copy(body.document),
        validation: copy(body.validation ?? null),
        versions: copy(body.versions ?? []),
      },
      show: copy(show),
      fingerprint: importedFingerprint,
    };
    projects.set(id, entry);
    return [entry, body.show != null && show === null ? [{ code: 'show_schema' }] : []];
  };

  const newId = () => { generated += 1; return `proj-fake${String(generated).padStart(8, '0')}`; };

  server.handle = (method, path, body) => {
    const [, id] = path.match(/^\/api\/projects(?:\/([^/?]*))?\/?(?:\?.*)?$/) ?? [];
    if (!path.startsWith('/api/projects')) return { status: 404, body: { detail: 'Not Found' } };

    if (id === undefined || id === '') {
      if (method === 'GET') return { status: 200, body: { projects: [...projects.values()].map((e) => view(e)) } };
      if (method === 'POST') {
        if (!validBody(body)) return INVALID;
        if (body.id != null && !ID_RE.test(body.id)) return fail(422, 'invalid_project');
        const wanted = body.id ?? newId();
        if (projects.has(wanted)) return fail(409, 'project_exists');
        const [entry, warnings] = store(wanted, body, 1);
        return { status: 201, body: saved(entry, warnings) };
      }
      return { status: 405, body: { detail: 'Method Not Allowed' } };
    }

    if (id === 'import' && method === 'POST') {
      if (!validBody(body)) return INVALID;
      const print = fingerprint(body);
      const already = [...projects.values()].find((e) => e.fingerprint === print);
      if (already) return { status: 200, body: saved(already, [], { status: 'skipped' }) };
      let status = 'created';
      let wanted = typeof body.id === 'string' && ID_RE.test(body.id) ? body.id : null;
      if (wanted && projects.has(wanted)) {
        wanted = `${wanted.slice(0, 55)}-${print}`;
        status = 'renamed';
        if (projects.has(wanted)) wanted = null;
      }
      const [entry, warnings] = store(wanted ?? newId(), body, 1, null, print);
      return { status: 200, body: saved(entry, warnings, { status }) };
    }

    const projectId = decodeURIComponent(id);
    if (!ID_RE.test(projectId)) return fail(422, 'invalid_project');
    const entry = projects.get(projectId);

    if (method === 'GET') {
      return entry ? { status: 200, body: { project: view(entry, true) } } : fail(404, 'project_not_found');
    }
    if (method === 'PUT') {
      if (!validBody(body) || !Number.isInteger(body.base_revision) || body.base_revision < 1) return INVALID;
      if (!entry) return fail(404, 'project_not_found');
      if (entry.meta.revision !== body.base_revision) {
        return fail(409, 'revision_conflict', { current_revision: entry.meta.revision });
      }
      const [next, warnings] = store(projectId, body, entry.meta.revision + 1, entry.meta.created_at, entry.fingerprint);
      return { status: 200, body: saved(next, warnings) };
    }
    if (method === 'DELETE') {
      if (!entry) return fail(404, 'project_not_found');
      projects.delete(projectId);
      return { status: 204, body: null };
    }
    return { status: 405, body: { detail: 'Method Not Allowed' } };
  };

  server.fetch = async (url, init = {}) => {
    const method = init.method ?? 'GET';
    const { pathname } = new URL(url, 'http://vault.test');
    server.requests.push({ method, path: pathname, body: init.body ? JSON.parse(init.body) : undefined });
    if (server.down) throw new TypeError('Failed to fetch');
    const { status, body } = server.handle(method, pathname, init.body ? JSON.parse(init.body) : undefined);
    return {
      status,
      ok: status >= 200 && status < 300,
      json: async () => { if (body === null) throw new SyntaxError('no body'); return copy(body); },
    };
  };

  /** What the vault holds, for assertions. */
  server.stored = (id) => {
    const entry = projects.get(id);
    return entry ? view(entry, true) : null;
  };
  server.ids = () => [...projects.keys()];
  return server;
}
