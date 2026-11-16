import { describe, expect, it } from 'vitest';
import contract from './vault-contract.json';
import { createFakeVaultServer } from './fakeVaultServer.js';

// vault-contract.json is recorded from the live backend by
// packages/tara-studio/tests/test_project_vault_contract.py, which also fails
// if the backend stops answering that way. Replaying it here is what makes
// every other test built on the fake a statement about the real API.

// The same masking the recorder applies: timestamps, and the content hash in a
// renamed import's id.
const mask = (value) => {
  if (Array.isArray(value)) return value.map(mask);
  if (value === null || typeof value !== 'object') return value;
  return Object.fromEntries(Object.entries(value).map(([key, v]) => {
    if (key === 'created_at' || key === 'updated_at') return [key, '<time>'];
    if (key === 'id' && typeof v === 'string') return [key, v.replace(/^(default-proj)-[0-9a-f]{8}$/, '$1-<fingerprint>')];
    return [key, mask(v)];
  }));
};

describe('the fake Project Vault matches the recorded backend', () => {
  const server = createFakeVaultServer();

  // One vault for the whole recording: each step sees what the earlier ones left.
  it.each(contract.steps.map((step) => [step.name, step]))('%s', (_name, { request, response }) => {
    const answer = server.handle(request.method, request.path, request.body ?? undefined);
    expect({ status: answer.status, body: mask(answer.body) }).toEqual(response);
  });
});

// The audit log's routes (tara_studio/audit_api.py). Not part of the recorded contract
// above, so these pin the stand-in's own behaviour against what audit_api.py answers.
describe('the fake Project Vault audit log', () => {
  const project = { name: 'Diwali', document: { scenes: [], dronesList: [] } };
  const entry = (n = 1) => ({
    v: 1, requestId: `r${n}`, source: 'typed', request: 'x', summary: 's', ops: [], decision: 'applied',
  });
  const withProject = () => {
    const server = createFakeVaultServer({ now: () => '2026-11-16T00:00:00.000Z' });
    server.handle('POST', '/api/projects', { ...project, id: 'diwali' });
    return server;
  };

  it('POST stores the entry with the server\'s seq, at and projectId and answers 201 {entry}', () => {
    const server = withProject();
    const a = server.handle('POST', '/api/projects/diwali/audit', entry(1));
    expect(a.status).toBe(201);
    expect(a.body.entry).toMatchObject({ ...entry(1), seq: 1, at: '2026-11-16T00:00:00.000Z', projectId: 'diwali' });
    expect(server.handle('POST', '/api/projects/diwali/audit', entry(2)).body.entry.seq).toBe(2);
    expect(server.audit('diwali').map((e) => e.requestId)).toEqual(['r1', 'r2']);
  });

  it('GET is newest first with the log state, and honours ?limit', () => {
    const server = withProject();
    for (let n = 1; n <= 5; n += 1) server.handle('POST', '/api/projects/diwali/audit', entry(n));
    const all = server.handle('GET', '/api/projects/diwali/audit');
    expect(all.status).toBe(200);
    expect(all.body).toMatchObject({ total: 5, full: false, chainOk: true, torn: false, corruptLines: 0 });
    expect(all.body.entries.map((e) => e.seq)).toEqual([5, 4, 3, 2, 1]);
    expect(server.handle('GET', '/api/projects/diwali/audit?limit=2').body.entries.map((e) => e.seq)).toEqual([5, 4]);
    expect(server.handle('GET', '/api/projects/diwali/audit?limit=2').body.total).toBe(5);
  });

  it('a bad limit is a 422 whose detail is a list with no code, like FastAPI\'s', () => {
    const server = withProject();
    for (const q of ['0', '501', 'x', '1.5']) {
      const r = server.handle('GET', `/api/projects/diwali/audit?limit=${q}`);
      expect(r.status).toBe(422);
      expect(Array.isArray(r.body.detail)).toBe(true);
    }
  });

  it('an unknown project is 404 project_not_found for both POST and GET, and nothing is created', () => {
    const server = createFakeVaultServer();
    for (const method of ['POST', 'GET']) {
      const r = server.handle(method, '/api/projects/nope/audit', entry());
      expect(r.status).toBe(404);
      expect(r.body.detail.code).toBe('project_not_found');
    }
    expect(server.ids()).toEqual([]);
  });

  it('an /audit POST never creates or changes a project', () => {
    const server = withProject();
    const before = server.stored('diwali');
    server.handle('POST', '/api/projects/diwali/audit', entry());
    server.handle('POST', '/api/projects/other/audit', { ...project, id: 'other' });
    expect(server.ids()).toEqual(['diwali']);
    expect(server.stored('diwali')).toEqual(before);
    expect(server.handle('GET', '/api/projects').body.projects).toHaveLength(1);
  });

  it('refuses what the backend refuses: server-owned fields, unknown fields, missing fields', () => {
    const server = withProject();
    for (const bad of [
      { ...entry(), seq: 9 }, { ...entry(), projectId: 'x' }, { ...entry(), hash: 'h' }, { ...entry(), at: 't' },
      { ...entry(), extra: 1 }, { v: 1 }, null, [],
    ]) {
      const r = server.handle('POST', '/api/projects/diwali/audit', bad);
      expect(r.status).toBe(422);
      expect(r.body.detail.code).toBe('invalid_entry');
    }
    expect(server.audit('diwali')).toEqual([]);
  });

  it('there is no PUT, PATCH or DELETE on the log', () => {
    const server = withProject();
    for (const method of ['PUT', 'PATCH', 'DELETE']) expect(server.handle(method, '/api/projects/diwali/audit', {}).status).toBe(405);
  });

  it('deleting the project takes its log with it; the same id starts a fresh log', () => {
    const server = withProject();
    server.handle('POST', '/api/projects/diwali/audit', entry());
    server.handle('DELETE', '/api/projects/diwali');
    expect(server.audit('diwali')).toEqual([]);
    server.handle('POST', '/api/projects', { ...project, id: 'diwali' });
    expect(server.handle('POST', '/api/projects/diwali/audit', entry()).body.entry.seq).toBe(1);
  });

  it('an odd project id is encoded in the path and decoded here; an invalid one is 422 invalid_project', () => {
    const server = createFakeVaultServer();
    expect(server.handle('GET', '/api/projects/a%20b/audit').body.detail.code).toBe('invalid_project');
    expect(server.handle('GET', '/api/projects/..%2Fx/audit').status).toBe(422);
  });

  it('works through fetch, with the query string', async () => {
    const server = withProject();
    await server.fetch('http://x/api/projects/diwali/audit', { method: 'POST', body: JSON.stringify(entry(1)) });
    await server.fetch('http://x/api/projects/diwali/audit', { method: 'POST', body: JSON.stringify(entry(2)) });
    const res = await server.fetch('http://x/api/projects/diwali/audit?limit=1');
    expect((await res.json()).entries.map((e) => e.seq)).toEqual([2]);
  });

  it('the project routes still answer as before (list, get, import)', () => {
    const server = withProject();
    expect(server.handle('GET', '/api/projects/diwali').body.project.id).toBe('diwali');
    expect(server.handle('GET', '/api/projects?x=1').body.projects).toHaveLength(1);
  });
});
