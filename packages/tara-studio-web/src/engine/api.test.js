import { afterEach, describe, expect, it, vi } from 'vitest';
import { API_BASE, GCS_BASE, sendShowToGCS } from './api.js';

const CHOREO = { drones: [{ id: 0, waypoints: [{ time: 0, x: 0, y: 0, z: 0, yaw: 0 }] }] };
const TRACK = [{ pattern: 'STROBE', start_time: 0, duration: 10, color: '#ffffff', frequency_hz: 8 }];

/** Stubs fetch with the Studio export reply, then the GCS upload reply. */
const stubBackends = () => {
  const calls = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    calls.push({ url, init });
    const body = url.startsWith(API_BASE)
      ? { filename: 'choreography.json', content: '{"version": 1, "drones": []}', mime_type: 'application/json' }
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
