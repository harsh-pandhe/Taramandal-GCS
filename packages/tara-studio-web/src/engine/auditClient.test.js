import { describe, expect, it, vi, afterEach } from 'vitest';
import { createAuditClient } from './auditClient.js';

const reply = (status, body) => ({
  status, ok: status >= 200 && status < 300,
  json: async () => { if (body === undefined) throw new SyntaxError('not json'); return body; },
});
const client = (impl, opts = {}) => {
  const fetchImpl = vi.fn(impl);
  return { fetchImpl, c: createAuditClient({ base: 'http://x', fetchImpl, ...opts }) };
};
const ENTRY = { v: 1, requestId: 'r1' };
const err = (status, code) => reply(status, { detail: { code, message: 'm' } });
const hangUntilAbort = (_url, { signal }) => new Promise((_res, rej) => {
  signal.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError')));
});

afterEach(() => vi.unstubAllGlobals());

describe('append', () => {
  it('201: the stored entry', async () => {
    const stored = { ...ENTRY, seq: 1, projectId: 'p' };
    const { c, fetchImpl } = client(async () => reply(201, { entry: stored }));
    expect(await c.append('p', ENTRY)).toEqual({ ok: true, entry: stored });
    const [target, init] = fetchImpl.mock.calls[0];
    expect(target).toBe('http://x/api/projects/p/audit');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' });
    expect(JSON.parse(init.body)).toEqual(ENTRY);
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });

  it.each([
    [404, 'project_not_found'], [409, 'audit_full'], [413, 'body_too_large'],
    [415, 'unsupported_media_type'], [422, 'invalid_entry'],
  ])('%i %s: the code, and the entry is known not to be stored', async (status, code) => {
    const { c } = client(async () => err(status, code));
    expect(await c.append('p', ENTRY)).toEqual({ ok: false, code, status, notStored: true });
  });

  it('503 vault_unavailable: the entry MAY be on disk, so notStored is false', async () => {
    const { c } = client(async () => err(503, 'vault_unavailable'));
    expect(await c.append('p', ENTRY)).toEqual({ ok: false, code: 'vault_unavailable', status: 503, notStored: false });
  });

  it('any other 5xx is not "not stored" either', async () => {
    const { c } = client(async () => reply(500, { detail: 'boom' }));
    expect(await c.append('p', ENTRY)).toEqual({ ok: false, code: 'unknown', status: 500, notStored: false });
  });

  it('a network failure: code network, may be stored', async () => {
    const { c } = client(async () => { throw new TypeError('Failed to fetch'); });
    expect(await c.append('p', ENTRY)).toEqual({ ok: false, code: 'network', status: 0, notStored: false });
  });

  it('a timeout aborts the request: code timeout, may be stored', async () => {
    const { c, fetchImpl } = client(hangUntilAbort, { timeoutMs: 20 });
    expect(await c.append('p', ENTRY)).toEqual({ ok: false, code: 'timeout', status: 0, notStored: false });
    expect(fetchImpl.mock.calls[0][1].signal.aborted).toBe(true);
  });

  it('a body that stalls after the headers is a timeout too', async () => {
    const { c } = client((_u, { signal }) => Promise.resolve({
      status: 201, ok: true,
      json: () => new Promise((_res, rej) => signal.addEventListener('abort', () => rej(new Error('aborted')))),
    }), { timeoutMs: 20 });
    expect((await c.append('p', ENTRY)).code).toBe('timeout');
  });

  it('a non-JSON body is "unknown", not a throw; a 4xx keeps its status', async () => {
    expect(await client(async () => reply(502, undefined)).c.append('p', ENTRY))
      .toEqual({ ok: false, code: 'unknown', status: 502, notStored: false });
    expect(await client(async () => reply(422, undefined)).c.append('p', ENTRY))
      .toEqual({ ok: false, code: 'unknown', status: 422, notStored: true });
  });

  it('a 201 whose body has no entry is a failure that may have been stored', async () => {
    expect(await client(async () => reply(201, undefined)).c.append('p', ENTRY))
      .toEqual({ ok: false, code: 'unknown', status: 201, notStored: false });
    expect(await client(async () => reply(201, { nope: 1 })).c.append('p', ENTRY)).toMatchObject({ ok: false, notStored: false });
  });

  it('puts the project id in the path encoded', async () => {
    const { c, fetchImpl } = client(async () => reply(201, { entry: {} }));
    await c.append('a b/c?d#e%', ENTRY);
    expect(fetchImpl.mock.calls[0][0]).toBe('http://x/api/projects/a%20b%2Fc%3Fd%23e%25/audit');
  });

  it('calls fetch exactly once, whatever happens: no retry', async () => {
    for (const impl of [
      async () => err(503, 'vault_unavailable'),
      async () => { throw new TypeError('x'); },
      async () => reply(500, undefined),
      async () => err(409, 'audit_full'),
    ]) {
      const { c, fetchImpl } = client(impl);
      await c.append('p', ENTRY);
      expect(fetchImpl).toHaveBeenCalledTimes(1);
    }
  });

  it('never throws: not for a missing project id, an entry that cannot be serialized, or an odd response', async () => {
    const { c, fetchImpl } = client(async () => reply(201, { entry: {} }));
    expect(await c.append(undefined, ENTRY)).toEqual({ ok: false, code: 'invalid_project', status: 0, notStored: true });
    expect(await c.append('', ENTRY)).toMatchObject({ ok: false, notStored: true });
    expect(fetchImpl).not.toHaveBeenCalled();
    const circular = {};
    circular.self = circular;
    expect(await c.append('p', circular)).toMatchObject({ ok: false, code: 'unknown' });
    expect(await client(async () => null).c.append('p', ENTRY)).toMatchObject({ ok: false, notStored: false });
  });

  it('uses the global fetch when none is injected, looked up at call time', async () => {
    const spy = vi.fn(async () => reply(201, { entry: { seq: 1 } }));
    vi.stubGlobal('fetch', spy);
    expect(await createAuditClient({ base: 'http://x' }).append('p', ENTRY)).toEqual({ ok: true, entry: { seq: 1 } });
    expect(spy).toHaveBeenCalledTimes(1);
  });
});

describe('list', () => {
  const OK = { entries: [{ seq: 2 }, { seq: 1 }], total: 2, full: false, chainOk: true, torn: false, corruptLines: 0 };

  it('the entries and the log state, newest first as sent', async () => {
    const { c, fetchImpl } = client(async () => reply(200, OK));
    expect(await c.list('p')).toEqual({ ok: true, ...OK });
    expect(fetchImpl.mock.calls[0][0]).toBe('http://x/api/projects/p/audit?limit=20');
    expect(fetchImpl.mock.calls[0][1].method).toBe('GET');
  });

  it('passes a limit through, clamped to what the backend accepts (so it never earns a 422)', async () => {
    const { c, fetchImpl } = client(async () => reply(200, OK));
    for (const limit of [5, 0, -3, 9999, 'x', 7.9, undefined]) await c.list('p', limit);
    expect(fetchImpl.mock.calls.map(([u]) => u.split('=')[1])).toEqual(['5', '20', '1', '500', '20', '7', '20']);
  });

  it('a full or damaged log is reported as it is', async () => {
    const { c } = client(async () => reply(200, { ...OK, full: true, chainOk: false, torn: true, corruptLines: 2 }));
    expect(await c.list('p')).toMatchObject({ full: true, chainOk: false, torn: true, corruptLines: 2 });
  });

  it('a missing chainOk is not read as intact', async () => {
    const { c } = client(async () => reply(200, { entries: [] }));
    expect(await c.list('p')).toMatchObject({ ok: true, entries: [], total: 0, chainOk: false, full: false });
  });

  it('a failure is the code, never a throw', async () => {
    expect(await client(async () => err(404, 'project_not_found')).c.list('p')).toEqual({ ok: false, code: 'project_not_found', status: 404 });
    expect(await client(async () => err(503, 'vault_unavailable')).c.list('p')).toEqual({ ok: false, code: 'vault_unavailable', status: 503 });
    expect(await client(async () => { throw new TypeError('x'); }).c.list('p')).toEqual({ ok: false, code: 'network', status: 0 });
    expect(await client(async () => reply(200, undefined)).c.list('p')).toEqual({ ok: false, code: 'unknown', status: 200 });
    expect(await client(async () => reply(200, { entries: 'no' })).c.list('p')).toMatchObject({ ok: false, code: 'unknown' });
  });

  it("FastAPI's own 422 for a bad limit has detail as a LIST and no code", async () => {
    const { c } = client(async () => reply(422, { detail: [{ type: 'less_than_equal', loc: ['query', 'limit'], msg: 'x' }] }));
    expect(await c.list('p')).toEqual({ ok: false, code: 'unknown', status: 422 });
  });

  it('times out, and encodes the project id', async () => {
    const hang = client(hangUntilAbort, { timeoutMs: 20 });
    expect(await hang.c.list('p')).toEqual({ ok: false, code: 'timeout', status: 0 });
    const odd = client(async () => reply(200, OK));
    await odd.c.list('a/b');
    expect(odd.fetchImpl.mock.calls[0][0]).toBe('http://x/api/projects/a%2Fb/audit?limit=20');
  });

  it('calls fetch exactly once, and never without a project id', async () => {
    const { c, fetchImpl } = client(async () => err(503, 'vault_unavailable'));
    await c.list('p');
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(await c.list(null)).toEqual({ ok: false, code: 'invalid_project', status: 0 });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});
