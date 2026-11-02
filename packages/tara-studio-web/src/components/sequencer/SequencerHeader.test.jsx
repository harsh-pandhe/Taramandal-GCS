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

  // The header states the export gate's verdict (issue #132) from the SAME
  // predicate the Transmit tab's buttons use (engine/exportGate.js). Review of
  // PR #210 found it saying "export open" while those buttons were locked.
  describe('validation / export state', () => {
    const PASSED = { status: 'PASSED', violations: { proximity: [], velocity: [], acceleration: [], jerk: [] } };
    const built = (props) => render({ builtDuration: 20, hasShow: true, ...props });

    it('says exports are enabled for a PASSED, certified validation of a built show', () => {
      const html = built({ validation: PASSED });
      expect(html).toContain('Safety passed · exports enabled');
      expect(html).not.toContain('locked');
    });

    it('says exports are locked for a FAILED validation', () => {
      const html = built({ validation: { ...PASSED, status: 'FAILED' } });
      expect(html).toContain('Safety failed · exports locked');
      expect(html).not.toContain('enabled');
    });

    it('does not say passed or enabled for PASSED with feasibility_certified false (the review finding)', () => {
      const html = built({ validation: { ...PASSED, feasibility_certified: false } });
      expect(html).toContain('Not flight-certified · exports locked');
      expect(html).not.toContain('Safety passed');
      expect(html).not.toContain('enabled');
    });

    it('does not say passed or enabled for PASSED with jerk violations', () => {
      const html = built({
        validation: { ...PASSED, violations: { ...PASSED.violations, jerk: [{ time: 3.1, drone: 2, jerk: 31 }] } },
      });
      expect(html).toContain('Not flight-certified · exports locked');
      expect(html).not.toContain('Safety passed');
      expect(html).not.toContain('enabled');
    });

    it('does not say passed or enabled for PASSED when no show is built', () => {
      const html = render({ builtDuration: 0, hasShow: false, validation: PASSED });
      expect(html).not.toContain('Safety passed');
      expect(html).not.toContain('enabled');
      expect(html).not.toContain('locked');
    });

    it('treats any other status as unverified and locked', () => {
      const html = built({ validation: { status: 'UNVERIFIED' } });
      expect(html).toContain('Unverified · exports locked');
    });

    it('shows a built show with no validation yet as unverified, not enabled', () => {
      const html = built({ validation: null });
      expect(html).toContain('Unverified · exports locked');
      expect(html).not.toContain('enabled');
    });

    it('shows Validating… while the sweep runs, even over a previous PASSED result', () => {
      const html = built({ validating: true, validation: PASSED });
      expect(html).toContain('Validating…');
      expect(html).not.toContain('enabled');
    });

    it('shows no validation badge before anything has been built', () => {
      const html = render({ builtDuration: 0, hasShow: false, validation: null });
      expect(html).not.toContain('export');
      expect(html).not.toContain('Validating');
    });
  });
});
