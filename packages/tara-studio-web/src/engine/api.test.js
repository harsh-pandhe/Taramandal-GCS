import { afterEach, describe, expect, it, vi } from 'vitest';
import { API_BASE, GCS_BASE, exportShow, sendShowToGCS } from './api.js';

const CHOREO = { drones: [{ id: 0, waypoints: [{ time: 0, x: 0, y: 0, z: 0, yaw: 0 }] }] };
const TRACK = [{ pattern: 'STROBE', start_time: 0, duration: 10, color: '#ffffff', frequency_hz: 8 }];

/** Stubs fetch with the Studio export reply, then the GCS upload reply.
 * Mirrors the real backend's PREVIEW_ONLY_ filename prefix (issue #161) so
 * a preview_only request through this stub exercises exportShow's guard
 * the same way a real backend response would. */
const stubBackends = () => {
  const calls = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    calls.push({ url, init });
    const previewOnly = url.startsWith(API_BASE) && JSON.parse(init.body).preview_only;
    const body = url.startsWith(API_BASE)
      ? {
        filename: `${previewOnly ? 'PREVIEW_ONLY_' : ''}choreography.json`,
        content: '{"version": 1, "drones": []}', mime_type: 'application/json',
      }
      : { status: 'success' };
    return new Response(JSON.stringify(body), { status: 200 });
  }));
  return calls;
};

afterEach(() => vi.unstubAllGlobals());

describe('sendShowToGCS (issue #55: transmit == download)', () => {
  it('exports with the LED track and rate, as the JSON download does', async () => {
    const calls = stubBackends();
    await sendShowToGCS({ choreo: CHOREO, homeLat: 1, homeLon: 2, ledTrack: TRACK, rateHz: 4 });

    expect(calls[0].url).toBe(`${API_BASE}/api/export`);
    const sent = JSON.parse(calls[0].init.body);
    expect(sent).toMatchObject({ format: 'json', led_track: TRACK, rate_hz: 4, choreo: CHOREO });
    expect(calls[1].url).toBe(`${GCS_BASE}/api/upload-trajectory`);
  });

  it('sends no led_track or rate_hz when there is no track', async () => {
    const calls = stubBackends();
    await sendShowToGCS({ choreo: CHOREO, homeLat: 1, homeLon: 2, ledTrack: null, rateHz: 0 });

    const sent = JSON.parse(calls[0].init.body);
    expect(sent).not.toHaveProperty('led_track');
    expect(sent).not.toHaveProperty('rate_hz');
  });
});

describe('exportShow preview_only (issue #161)', () => {
  it('sends preview_only: true when requested', async () => {
    const calls = stubBackends();
    await exportShow({ choreo: CHOREO, format: 'json', homeLat: 1, homeLon: 2 });
    const sentNormal = JSON.parse(calls[0].init.body);
    expect(sentNormal).not.toHaveProperty('preview_only');

    await exportShow({ choreo: CHOREO, format: 'json', homeLat: 1, homeLon: 2, previewOnly: true });
    const sentPreview = JSON.parse(calls[1].init.body);
    expect(sentPreview).toMatchObject({ preview_only: true });
  });

  it('surfaces the backend detail message on a refused preview export', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ detail: 'Export blocked: altitude ceiling exceeded.' }), { status: 412 },
    )));
    await expect(exportShow({ choreo: CHOREO, format: 'json', homeLat: 1, homeLon: 2, previewOnly: true }))
      .rejects.toThrow('Export blocked: altitude ceiling exceeded.');
  });

  it('unwraps an object detail instead of surfacing "[object Object]"', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ detail: { message: 'Export blocked: unknown validation status.', validation: {} } }),
      { status: 412 },
    )));
    await expect(exportShow({ choreo: CHOREO, format: 'json', homeLat: 1, homeLon: 2, previewOnly: true }))
      .rejects.toThrow('Export blocked: unknown validation status.');
  });

  it('passes the backend\'s PREVIEW_ONLY_-prefixed filename straight through', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      const { preview_only } = JSON.parse(init.body);
      const filename = preview_only ? 'PREVIEW_ONLY_choreography.json' : 'choreography.json';
      return new Response(JSON.stringify({ filename, content: '{}', mime_type: 'application/json' }), { status: 200 });
    }));
    const data = await exportShow({ choreo: CHOREO, format: 'json', homeLat: 1, homeLon: 2, previewOnly: true });
    expect(data.filename).toBe('PREVIEW_ONLY_choreography.json');
  });

  it('refuses a response without the PREVIEW_ONLY_ prefix rather than downloading it', async () => {
    // Simulates an older/misconfigured backend that ran a normal export
    // anyway - FastAPI would silently ignore an unknown preview_only field.
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ filename: 'choreography.json', content: '{}', mime_type: 'application/json' }),
      { status: 200 },
    )));
    await expect(exportShow({ choreo: CHOREO, format: 'json', homeLat: 1, homeLon: 2, previewOnly: true }))
      .rejects.toThrow(/PREVIEW_ONLY_/);
  });

  it('does not apply the PREVIEW_ONLY_ check to a normal (non-preview) export', async () => {
    const calls = stubBackends();
    const data = await exportShow({ choreo: CHOREO, format: 'json', homeLat: 1, homeLon: 2 });
    expect(data.filename).toBe('choreography.json');
    expect(calls).toHaveLength(1);
  });
});
