import { describe, expect, it } from 'vitest';
import {
  describeSent, describeFailure, readableDetail, clip, formatTime, sameInputs, TITLE_MAX, DETAIL_MAX,
} from './transmitResult.js';

const NOW = new Date(2026, 10, 17, 9, 5, 30).getTime();
const GCS = 'http://localhost:8090';
const TOKEN = 'tok_9f8e7d6c5b4a39281706f5e4d3c2b1a0';

// Shapes copied from packages/tara-gcs/tara_gcs/main.py (upload_trajectory, require(), _check_command,
// _check_no_active_single_drone_sequence), as sendShowToGCS hands them over.
const http = (status, detail) => Object.assign(new Error('x'), { status, detail });
const allText = (vm) => JSON.stringify(vm);

describe('describeSent', () => {
  const body = {
    status: 'success',
    message: 'Trajectory uploaded and saved to active slot.',
    drones_summary: { 0: { waypoints_count: 4, duration_seconds: 9 }, 1: { waypoints_count: 4, duration_seconds: 9 } },
    trajectory_data: { 0: [{ time: 0 }] },
    signature: { verified: true, signer: 'Studio laptop', key_id: 'ab12cd34ef567890', signed_at: '2026-11-17T09:00:00Z' },
    site: 'standard',
    geofence: { soft_warning_radius_m: 1, hard_geofence_radius_m: 2 },
  };

  it('says how many vehicles went to which GCS, and keeps the old launch-check advice', () => {
    const vm = describeSent(body, { droneCount: 24, gcs: GCS, now: NOW });
    expect(vm.kind).toBe('sent');
    expect(vm.title).toBe('Sent the show for 2 vehicles to the GCS at localhost:8090.');
    expect(vm.detail).toContain('Run launch checks on the GCS dashboard.');
    expect(vm.detail).toContain('Signature verified (signer: Studio laptop).');
    expect(vm.at).toBe(NOW);
  });

  it('falls back to the Studio fleet size when the response has no per-drone summary', () => {
    expect(describeSent({}, { droneCount: 1, gcs: GCS }).title).toBe('Sent the show for 1 vehicle to the GCS at localhost:8090.');
    expect(describeSent(null, { droneCount: null, gcs: GCS }).title).toBe('Sent the show to the GCS at localhost:8090.');
    expect(describeSent(undefined).title).toBe('Sent the show to the GCS.');
  });

  it('reports an unsigned acceptance as such', () => {
    const vm = describeSent({ drones_summary: { 0: {} }, signature: { verified: false } }, { gcs: GCS });
    expect(vm.detail).toBe('The GCS accepted it without a signature. Run launch checks on the GCS dashboard.');
  });

  it('never echoes the trajectory, the key id or any other field of the response', () => {
    const vm = describeSent({ ...body, token: TOKEN, authorization: `Bearer ${TOKEN}` }, { droneCount: 2, gcs: GCS });
    expect(allText(vm)).not.toContain(TOKEN);
    expect(allText(vm)).not.toContain('ab12cd34ef567890');
    expect(allText(vm)).not.toContain('waypoints_count');
  });

  it('never throws, whatever the body is', () => {
    const hostile = { get drones_summary() { throw new Error('boom'); } };
    expect(() => describeSent(hostile, { gcs: GCS })).not.toThrow();
    expect(describeSent(hostile, { gcs: GCS }).kind).toBe('sent');
    expect(() => describeSent('text', { gcs: 'not a url' })).not.toThrow();
  });
});

describe('describeFailure, from the real GCS answers', () => {
  it('401: the sign-in advice the Studio already gives is the reason', () => {
    const e = Object.assign(new Error('The GCS needs an operator sign-in: use "Sign in to GCS" in this tab, then transmit again.'), { status: 401 });
    const vm = describeFailure(e, { now: NOW });
    expect(vm).toMatchObject({ kind: 'refused', at: NOW });
    expect(vm.title).toBe('Not sent: the GCS refused the show (HTTP 401).');
    expect(vm.detail).toContain('Sign in to GCS');
  });

  it('403 role refusal (string detail)', () => {
    const vm = describeFailure(http(403, 'viewer1 is a VIEWER; this needs CHOREOGRAPHER or higher.'));
    expect(vm.kind).toBe('refused');
    expect(vm.title).toContain('HTTP 403');
    expect(vm.detail).toBe('viewer1 is a VIEWER; this needs CHOREOGRAPHER or higher.');
  });

  it('403 signature refusal (string detail)', () => {
    const vm = describeFailure(http(403, 'Show file rejected: trajectory is unsigned. Upload it together with its .sig.json signature manifest.'));
    expect(vm.kind).toBe('refused');
    expect(vm.detail).toContain('Show file rejected: trajectory is unsigned.');
  });

  it('409 fleet-state refusal (object detail with a message)', () => {
    const detail = {
      error: 'illegal_transition', command: 'upload_trajectory', state: 'RUNNING', allowed_from: ['IDLE'],
      message: 'Cannot upload_trajectory while the fleet is RUNNING.',
    };
    const vm = describeFailure(http(409, detail));
    expect(vm.kind).toBe('refused');
    expect(vm.detail).toBe('Cannot upload_trajectory while the fleet is RUNNING.');
    expect(allText(vm)).not.toContain('allowed_from');
    expect(vm.detail).not.toContain('{');
  });

  it('409 single-drone-sequence refusal (string detail)', () => {
    const vm = describeFailure(http(409, 'Cannot upload a trajectory while a single-drone sequence is active on drone(s) [1].'));
    expect(vm.detail).toContain('single-drone sequence is active');
  });

  it('400 too large and parse failures (string detail)', () => {
    expect(describeFailure(http(400, 'File too large. Maximum size is 5MB.')).detail).toBe('File too large. Maximum size is 5MB.');
  });

  it('412 gate refusal (object detail with a message)', () => {
    const vm = describeFailure(http(412, { message: 'Physical flight action blocked: gate #135.', validation: { status: 'FAILED' } }));
    expect(vm).toMatchObject({ kind: 'refused' });
    expect(vm.detail).toBe('Physical flight action blocked: gate #135.');
    expect(allText(vm)).not.toContain('validation');
  });

  it('422 validation errors (list detail) read as field: reason, not as JSON', () => {
    const vm = describeFailure(http(422, [
      { loc: ['body', 'file'], msg: 'Field required', type: 'missing' },
      { loc: ['body', 'signature'], msg: 'Input should be a valid file', type: 'x' },
    ]));
    expect(vm.kind).toBe('refused');
    expect(vm.detail).toBe('file: Field required; signature: Input should be a valid file');
    expect(vm.detail).not.toContain('{');
    expect(vm.detail).not.toContain('missing');
  });

  it('a long list is cut to three reasons plus a count', () => {
    const detail = Array.from({ length: 5 }, (_, i) => ({ loc: ['body', `f${i}`], msg: `bad ${i}` }));
    expect(describeFailure(http(422, detail)).detail).toBe('f0: bad 0; f1: bad 1; f2: bad 2 (+2 more)');
  });

  it('5xx is a GCS error, "failed", with the reason when there is one', () => {
    const vm = describeFailure(http(500, 'Internal Server Error'));
    expect(vm).toMatchObject({ kind: 'failed', title: 'Not sent: the GCS reported an error (HTTP 500).', detail: 'Internal Server Error' });
  });

  it('a missing reason says so instead of printing nothing or undefined', () => {
    const missing = Object.assign(new Error(''), { status: 403 });
    expect(describeFailure(missing).detail).toBe('The GCS gave no reason.');
    expect(describeFailure(http(503, undefined)).detail).not.toContain('undefined');
    expect(describeFailure(http(403, {})).detail).toBe('x');
    expect(describeFailure(http(403, 42)).detail).toBe('x');
    expect(describeFailure(http(403, null)).detail).toBe('x');
  });

  it('a failure before the GCS was involved (the Studio could not prepare the show)', () => {
    const vm = describeFailure(new Error('File export failed.'));
    expect(vm).toMatchObject({ kind: 'failed', title: 'Not sent: the transmit failed.', detail: 'File export failed.' });
  });

  it('network error: the GCS could not be reached and nothing is confirmed as received', () => {
    const vm = describeFailure(Object.assign(new Error('Failed to fetch'), { gcsUnreachable: true, timedOut: false }));
    expect(vm.kind).toBe('failed');
    expect(vm.title).toBe('The GCS could not be reached.');
    expect(vm.detail).toContain('Nothing is confirmed as received.');
    expect(allText(vm)).not.toContain('Failed to fetch');
  });

  it('timeout says so, with the same assurance about what was received', () => {
    const vm = describeFailure(Object.assign(new Error('The operation timed out'), { gcsUnreachable: true, timedOut: true }));
    expect(vm.title).toContain('could not be reached');
    expect(vm.title).toContain('did not answer in time');
    expect(vm.detail).toContain('Nothing is confirmed as received.');
  });

  it('never throws on odd errors', () => {
    for (const odd of [undefined, null, 'text', 7, {}, { status: 'x' }, { get message() { throw new Error('x'); } }]) {
      expect(() => describeFailure(odd)).not.toThrow();
      expect(describeFailure(odd).kind).toBe('failed');
    }
  });
});

describe('what never reaches the screen', () => {
  it('a body with a token-like field is not echoed, whatever its shape', () => {
    const e = http(403, { message: 'No.', token: TOKEN, session: { token: TOKEN }, signature: TOKEN, private_key: TOKEN });
    expect(allText(describeFailure(e))).not.toContain(TOKEN);
    const list = http(422, [{ msg: 'bad', loc: ['x'], input: TOKEN, ctx: { token: TOKEN } }]);
    expect(allText(describeFailure(list))).not.toContain(TOKEN);
  });

  it('a Bearer header that slipped into a reason is masked', () => {
    const vm = describeFailure(http(403, `Bad header Authorization: Bearer ${TOKEN} sent`));
    expect(allText(vm)).not.toContain(TOKEN);
    expect(vm.detail).toContain('Bearer …');
  });

  it('control characters and line breaks become single spaces', () => {
    expect(readableDetail('a\n\n b\u0000\tc')).toBe('a b c');
  });
});

describe('cut by characters', () => {
  it('clip counts code points, so Devanagari and emoji are not split', () => {
    const hindi = 'सुरक्षा'.repeat(100);
    const cut = clip(hindi, 50);
    expect([...cut]).toHaveLength(50);
    expect(cut.endsWith('…')).toBe(true);
    expect(clip('😀'.repeat(10), 5)).toBe('😀😀😀😀…');
    expect(clip('short', 50)).toBe('short');
  });

  it('a very long reason is cut to the detail limit, a very long title to the title limit', () => {
    const vm = describeFailure(http(403, 'सुरक्षा '.repeat(500)));
    expect([...vm.detail].length).toBeLessThanOrEqual(DETAIL_MAX);
    expect(vm.detail.endsWith('…')).toBe(true);
    expect([...vm.title].length).toBeLessThanOrEqual(TITLE_MAX);
    const long = describeSent({ signature: { verified: true, signer: 'x'.repeat(1000) } }, { gcs: GCS });
    expect([...long.detail].length).toBeLessThanOrEqual(DETAIL_MAX);
  });
});

describe('formatTime and sameInputs', () => {
  it('formats local HH:MM', () => {
    expect(formatTime(NOW)).toBe('09:05');
    expect(formatTime(new Date(2026, 0, 1, 23, 59).getTime())).toBe('23:59');
    expect(formatTime(NaN)).toBe('');
  });

  it('sameInputs is false as soon as any one input is a different object or value', () => {
    const show = { id: 1 }; const track = [];
    const base = [show, track, 'perDrone', '#fff', 12.9, 77.5, 10];
    expect(sameInputs(base, [...base])).toBe(true);
    expect(sameInputs(base, [{ id: 1 }, track, 'perDrone', '#fff', 12.9, 77.5, 10])).toBe(false);   // rebuilt show
    expect(sameInputs(base, [show, [], 'perDrone', '#fff', 12.9, 77.5, 10])).toBe(false);          // new LED track
    expect(sameInputs(base, [show, track, 'perDrone', '#fff', 12.9, 77.5, 4])).toBe(false);        // rate
    expect(sameInputs(base, base.slice(1))).toBe(false);
    expect(sameInputs(null, base)).toBe(false);
  });
});
