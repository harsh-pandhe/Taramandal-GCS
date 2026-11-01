import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import SequencerHeader from './SequencerHeader.jsx';

const render = (props = {}) => renderToStaticMarkup(
  <SequencerHeader
    sceneCount={3} numDrones={10} rate={4}
    predictedDuration={12.5} builtDuration={0} stale={false}
    predictionRefused={false} refusalMessage={null}
    {...props}
  />
);

describe('SequencerHeader', () => {
  it('shows the predicted duration normally', () => {
    const html = render({ predictedDuration: 12.5 });
    expect(html).toContain('12.5s');
    expect(html).not.toContain('Refused');
  });

  it('shows "—" and a Refused badge instead of "0.0s" when the scene list cannot be built (issue #141 review)', () => {
    // predictedDuration is always 0 on a refusal (App.jsx's catch), which on
    // its own reads as "a real, instant show" rather than "no prediction".
    const html = render({ predictedDuration: 0, predictionRefused: true, refusalMessage: 'too close' });
    expect(html).toContain('—');
    expect(html).not.toContain('0.0s');
    expect(html).toContain('Refused');
    expect(html).toContain('too close');
  });

  it('refused takes priority over stale and built badges', () => {
    const html = render({
      predictedDuration: 0, predictionRefused: true, stale: true, builtDuration: 20,
    });
    expect(html).toContain('Refused');
    expect(html).not.toContain('Rebuild needed');
    expect(html).not.toContain('Built ·');
  });

  it('falls back to "The current scene list cannot be built" when no message is given', () => {
    const html = render({ predictedDuration: 0, predictionRefused: true, refusalMessage: null });
    expect(html).toContain('The current scene list cannot be built');
  });

  it('still shows the stale badge when not refused', () => {
    const html = render({ predictionRefused: false, stale: true });
    expect(html).toContain('Rebuild needed');
  });

  it('still shows the built badge when not refused and not stale', () => {
    const html = render({ predictionRefused: false, stale: false, builtDuration: 20 });
    expect(html).toContain('Built · 20.0s');
  });

  // The header states the export gate's verdict (issue #132), mirroring
  // App.jsx: only a PASSED validation opens export.
  describe('validation / export state', () => {
    it('says export is open only for a PASSED validation', () => {
      const html = render({ builtDuration: 20, validation: { status: 'PASSED' } });
      expect(html).toContain('Safety passed · export open');
    });

    it('says export is blocked for a FAILED validation', () => {
      const html = render({ builtDuration: 20, validation: { status: 'FAILED' } });
      expect(html).toContain('Safety failed · export blocked');
      expect(html).not.toContain('export open');
    });

    it('treats any other status as unverified and blocked', () => {
      const html = render({ builtDuration: 20, validation: { status: 'UNVERIFIED' } });
      expect(html).toContain('Unverified · export blocked');
    });

    it('shows a built show that has no validation yet as unverified, not as open', () => {
      const html = render({ builtDuration: 20, validation: null });
      expect(html).toContain('Unverified · export blocked');
      expect(html).not.toContain('export open');
    });

    it('shows Validating… while the sweep runs, even over a previous PASSED result', () => {
      const html = render({ builtDuration: 20, validating: true, validation: { status: 'PASSED' } });
      expect(html).toContain('Validating…');
      expect(html).not.toContain('export open');
    });

    it('shows no validation badge before anything has been built', () => {
      const html = render({ builtDuration: 0, validation: null });
      expect(html).not.toContain('export');
      expect(html).not.toContain('Validating');
    });
  });
});
