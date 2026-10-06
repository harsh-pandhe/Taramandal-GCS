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
