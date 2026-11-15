import { describe, expect, it, vi } from 'vitest';
import { textOf } from '../../testing/elementTree.js';

// Issue #282, items 2-4: what the audio panel says about an analysis. Same no-DOM approach as the other
// panel tests: the component is called as a function with useRef stubbed.
vi.mock('react', async (orig) => ({ ...(await orig()), useRef: (v) => ({ current: v }) }));
const { default: AudioPanel } = await import('./AudioPanel.jsx');

// The analyser's real response shape: beats are { t, confidence }, the confidence is bpm_confidence,
// bpm is null when no tempo is found, and there is no `sections` field.
const analysis = (over = {}) => ({
  analyzer_version: '1', duration_s: 80, bpm: 120, bpm_confidence: 0.9,
  beats: [{ t: 0.5, confidence: 0.5 }, { t: 1, confidence: 0.5 }],
  onsets: [], bars: { beats_per_bar: 4, downbeat_beat_index: 0, confidence: 0.5, times: [0.5, 2.5] },
  ...over,
});
const render = (a) => textOf(AudioPanel({
  audio: {
    hasTrack: true, track: { name: 't.wav', duration: 80 }, analysis: a, analyzing: false,
    load: vi.fn(), clear: vi.fn(), setVolume: vi.fn(), analyzeTrack: vi.fn(),
  },
  zoom: 1, onZoom: vi.fn(), ledTrack: [], onLedTrack: vi.fn(), duration: 80, currentTime: 0, onScrub: vi.fn(),
}));

describe('AudioPanel analysis summary', () => {
  it('shows the tempo and the real confidence (bpm_confidence), never 0%', () => {
    const text = render(analysis());
    expect(text).toContain('120.0 BPM');
    expect(text).toContain('90% conf');
    expect(text).toContain('Beats: 2');
    expect(text).toContain('Bars: 2');
    expect(text).not.toMatch(/(?<!\d)0% conf/); // a standalone 0%, as before the fix
  });

  it('says no tempo was found instead of "undefined BPM" when bpm is null', () => {
    const text = render(analysis({ bpm: null, bpm_confidence: 0 }));
    expect(text).toContain('No tempo found');
    expect(text).not.toMatch(/undefined/);
    expect(text).not.toContain('NaN');
    expect(text).not.toContain('% conf'); // a confidence in a tempo that was not found would mislead
  });

  it('has no Sections count (the analyser has no such field)', () => {
    expect(render(analysis())).not.toContain('Sections');
    expect(render(analysis({ sections: [{}, {}] }))).not.toContain('Sections');
  });
});
