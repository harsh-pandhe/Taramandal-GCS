import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  API_BASE,
  GCS_BASE,
  analyzeAudioTrack,
  convertRasterToFormation,
  exportShow,
  proposePlan,
  proposeScene,
  sendShowToGCS,
} from './api.js';

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

describe('proposePlan (issue #94)', () => {
  it('posts prompt, scenes and repair_note to /api/ai/plan and returns the body as-is', async () => {
    const calls = [];
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ status: 'ok', ops: [] }), { status: 200 });
    }));
    const scenes = [{ number: 1, name: 'A', shape: 'circle', scale: 1, hold_seconds: 6, transition_seconds: 4 }];
    const result = await proposePlan({ prompt: 'p', scenes, repairNote: 'why' });
    expect(calls[0].url).toBe(`${API_BASE}/api/ai/plan`);
    expect(JSON.parse(calls[0].init.body)).toEqual({ prompt: 'p', scenes, repair_note: 'why' });
    expect(result).toEqual({ status: 'ok', ops: [] });
  });

  it('sends repair_note: null on a first call', async () => {
    const calls = [];
    vi.stubGlobal('fetch', vi.fn(async (url, init) => { calls.push(init); return new Response('{}', { status: 200 }); }));
    await proposePlan({ prompt: 'p', scenes: [] });
    expect(JSON.parse(calls[0].body).repair_note).toBeNull();
  });

  it('rejects on a non-ok response', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: [] }), { status: 422 })));
    await expect(proposePlan({ prompt: 'p', scenes: [] })).rejects.toThrow(/422/);
  });
});

describe('proposeScene (issue #133)', () => {
  it('posts the prompt and svg, and returns the typed draft as-is', async () => {
    const calls = [];
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ status: 'ok', name: 'Heart', shape_type: 'parametric', shape_payload: 'heart' }), { status: 200 });
    }));

    const result = await proposeScene('a heart', '<svg></svg>');

    expect(calls[0].url).toBe(`${API_BASE}/api/ai/propose`);
    expect(JSON.parse(calls[0].init.body)).toEqual({ prompt: 'a heart', svg: '<svg></svg>' });
    expect(result).toEqual({ status: 'ok', name: 'Heart', shape_type: 'parametric', shape_payload: 'heart' });
  });

  it('sends svg: null when no SVG was attached', async () => {
    const calls = [];
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ status: 'ok' }), { status: 200 });
    }));

    await proposeScene('a heart');
    expect(JSON.parse(calls[0].init.body).svg).toBeNull();
  });

  it('throws with the backend detail on a non-ok response (a malformed request, not a model failure)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ detail: 'prompt too long' }), { status: 422 },
    )));
    await expect(proposeScene('x')).rejects.toThrow(/prompt too long/);
  });
});

describe('convertRasterToFormation (issue #90)', () => {
  it('posts image_b64 and parameters to /api/raster-formation', async () => {
    const calls = [];
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ status: 'success', drone_count: 10, coordinates: [] }), { status: 200 });
    }));

    const res = await convertRasterToFormation({
      imageB64: 'abc123==',
      nDrones: 10,
      scale: 25.0,
      centerX: 5.0,
      altitude: 18.0,
    });

    expect(calls[0].url).toBe(`${API_BASE}/api/raster-formation`);
    expect(JSON.parse(calls[0].init.body)).toMatchObject({
      image_b64: 'abc123==',
      n_drones: 10,
      scale: 25.0,
      center_x: 5.0,
      altitude: 18.0,
    });
    expect(res.status).toBe('success');
  });

  it('throws with backend detail on error', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ detail: 'unsupported image format' }), { status: 400 },
    )));
    await expect(convertRasterToFormation({ imageB64: 'bad', nDrones: 5 }))
      .rejects.toThrow(/unsupported image format/);
  });
});

describe('analyzeAudioTrack (issue #91)', () => {
  it('posts wav_b64 and parameters to /api/music/analyze', async () => {
    const calls = [];
    vi.stubGlobal('fetch', vi.fn(async (url, init) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ analyzer_version: '1', bpm: 120, beats: [] }), { status: 200 });
    }));

    const res = await analyzeAudioTrack({
      wavB64: 'riff123==',
      minBpm: 70.0,
      maxBpm: 180.0,
      beatsPerBar: 3,
    });

    expect(calls[0].url).toBe(`${API_BASE}/api/music/analyze`);
    expect(JSON.parse(calls[0].init.body)).toEqual({
      wav_b64: 'riff123==',
      min_bpm: 70.0,
      max_bpm: 180.0,
      beats_per_bar: 3,
    });
    expect(res.bpm).toBe(120);
  });

  it('throws with backend detail on error', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ detail: 'silent: the audio is silent' }), { status: 400 },
    )));
    await expect(analyzeAudioTrack({ wavB64: 'silent' }))
      .rejects.toThrow(/silent: the audio is silent/);
  });
});

