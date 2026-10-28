import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import TransmitPanel from './TransmitPanel.jsx';
import { previewExportSuccessMessage } from '../../engine/api.js';

const noop = () => {};

const render = (props = {}) => renderToStaticMarkup(
  <TransmitPanel
    homeLat={12.9716}
    homeLon={77.5946}
    onHomeLat={noop}
    onHomeLon={noop}
    onExport={noop}
    onExportPreview={noop}
    onSendToGCS={noop}
    sending={false}
    hasShow={true}
    validation={{ status: 'PASSED', violations: { proximity: [], velocity: [], acceleration: [], yaw_rate: [] } }}
    {...props}
  />
);

const count = (html, re) => (html.match(re) || []).length;

describe('TransmitPanel Hard Safety Gate (Issue #139)', () => {
  it('enables export and transmit buttons when validation status is PASSED', () => {
    const html = render({
      hasShow: true,
      validation: {
        status: 'PASSED',
        violations: { proximity: [], velocity: [], acceleration: [], yaw_rate: [] },
      },
    });

    expect(html).toContain('safety-gate-badge passed');
    expect(html).toContain('Safety Certified');
    expect(html).not.toContain('Validation Failed');

    // Action buttons (JSON, CSV, tara, KML, Transmit) should NOT be disabled
    // (Only GCS sign-in submit button might be disabled when fields are empty)
    const exportSection = html.slice(html.indexOf('Export &amp; Transmit'));
    // All 4 export buttons + Transmit button are enabled
    expect(count(exportSection, /<button[^>]*disabled/g)).toBe(1); // Only the GcsSignIn submit button
  });

  it('locks all export and transmit buttons when validation status is FAILED', () => {
    const html = render({
      hasShow: true,
      validation: {
        status: 'FAILED',
        violations: {
          proximity: [{ time: 1.0, drones: [0, 1], distance: 1.5 }],
          velocity: [],
          acceleration: [],
          yaw_rate: [],
        },
      },
    });

    expect(html).toContain('safety-gate-badge failed');
    expect(html).toContain('Safety Gate Locked');
    expect(html).toContain('1 violation detected');

    // JSON, CSV, tara, KML, and Transmit buttons must all be disabled
    const exportSection = html.slice(html.indexOf('Export &amp; Transmit'));
    const disabledCount = count(exportSection, /<button[^>]*disabled/g);
    expect(disabledCount).toBeGreaterThanOrEqual(5); // JSON, CSV, tara, KML, GCS Transmit, plus GcsSignIn
  });

  it('locks all export and transmit buttons when show is unverified or validation is missing', () => {
    const html = render({
      hasShow: true,
      validation: null,
    });

    expect(html).toContain('safety-gate-badge unverified');
    expect(html).toContain('Unverified Trajectory');

    const exportSection = html.slice(html.indexOf('Export &amp; Transmit'));
    const disabledCount = count(exportSection, /<button[^>]*disabled/g);
    expect(disabledCount).toBeGreaterThanOrEqual(5);
  });

  it('shows a validating message instead of "required" while validation is actually in flight (issue #173)', () => {
    // validation is null either way (not yet run vs. currently running), so
    // without `validating` an operator waiting on a slow validate at a large
    // fleet size would read the exact same "required" wording as someone who
    // hasn't triggered it at all - still locked either way, just worded
    // differently so a legitimate wait doesn't look like nothing happened.
    const html = render({
      hasShow: true,
      validation: null,
      validating: true,
    });

    expect(html).toContain('safety-gate-badge unverified');
    expect(html).toContain('Validating');
    expect(html).not.toContain('Unverified Trajectory');

    const exportSection = html.slice(html.indexOf('Export &amp; Transmit'));
    const disabledCount = count(exportSection, /<button[^>]*disabled/g);
    expect(disabledCount).toBeGreaterThanOrEqual(5);
  });

  it('locks all export and transmit buttons when validator returned UNVERIFIED status', () => {
    const html = render({
      hasShow: true,
      validation: { status: 'UNVERIFIED', summary: 'Validator service unavailable' },
    });

    expect(html).toContain('safety-gate-badge unverified');
    expect(html).toContain('Unverified Trajectory');

    const exportSection = html.slice(html.indexOf('Export &amp; Transmit'));
    const disabledCount = count(exportSection, /<button[^>]*disabled/g);
    expect(disabledCount).toBeGreaterThanOrEqual(5);
  });

  it('locks all export and transmit buttons when core validation passed but jerk violations exist', () => {
    const html = render({
      hasShow: true,
      validation: {
        status: 'PASSED',
        feasibility_certified: false,
        violations: {
          proximity: [],
          velocity: [],
          acceleration: [],
          yaw_rate: [],
          jerk: [{ time: 1.5, drone: 0, jerk: 35.0, limit: 20.0 }],
        },
      },
    });

    expect(html).toContain('safety-gate-badge failed');
    expect(html).toContain('Safety Gate Locked');
    expect(html).toContain('1 violation detected');

    const exportSection = html.slice(html.indexOf('Export &amp; Transmit'));
    const disabledCount = count(exportSection, /<button[^>]*disabled/g);
    expect(disabledCount).toBeGreaterThanOrEqual(5);
  });

  it('locks all export and transmit buttons when core validation passed but feasibility_certified is false', () => {
    const html = render({
      hasShow: true,
      validation: {
        status: 'PASSED',
        feasibility_certified: false,
        violations: { proximity: [], velocity: [], acceleration: [], yaw_rate: [] },
      },
    });

    expect(html).toContain('safety-gate-badge failed');
    expect(html).toContain('Safety Gate Locked');

    const exportSection = html.slice(html.indexOf('Export &amp; Transmit'));
    const disabledCount = count(exportSection, /<button[^>]*disabled/g);
    expect(disabledCount).toBeGreaterThanOrEqual(5);
  });

  it('disables buttons when there is no built show', () => {
    const html = render({
      hasShow: false,
      validation: null,
    });

    expect(html).not.toContain('safety-gate-badge');
    const exportSection = html.slice(html.indexOf('Export &amp; Transmit'));
    expect(count(exportSection, /<button[^>]*disabled/g)).toBeGreaterThanOrEqual(5);
  });
});

describe('Unsigned preview export (issue #161)', () => {
  it('offers a preview export when jerk is the sole reason export is refused', () => {
    const html = render({
      hasShow: true,
      validation: {
        status: 'PASSED',
        feasibility_certified: false,
        violations: {
          proximity: [], velocity: [], acceleration: [], yaw_rate: [],
          jerk: [{ time: 1.5, drone: 0, jerk: 35.0, limit: 20.0 }],
        },
      },
    });
    expect(html).toContain('preview-export-notice');
    expect(html).toContain('Preview JSON');
    expect(html).toContain('Preview CSV');
    expect(html).toContain('Preview .tara');
    // The preview buttons are never disabled by the safety gate - that's the
    // entire point of offering them when the gate is locked.
    const previewSection = html.slice(html.indexOf('preview-export-notice'), html.indexOf('Export &amp; Transmit'));
    expect(previewSection).not.toMatch(/<button[^>]*disabled/);
  });

  it('does not offer a preview export when a hard safety limit also fails', () => {
    const html = render({
      hasShow: true,
      validation: {
        status: 'FAILED',
        violations: {
          proximity: [{ time: 1.0, drones: [0, 1], distance: 1.5 }],
          velocity: [], acceleration: [], yaw_rate: [],
          jerk: [{ time: 1.5, drone: 0, jerk: 35.0, limit: 20.0 }],
        },
      },
    });
    // preview_only still enforces proximity server-side, so offering it here
    // would just trade one refusal for another - not useful.
    expect(html).not.toContain('preview-export-notice');
  });

  it('does not offer a preview export when the show is certified', () => {
    const html = render({
      hasShow: true,
      validation: { status: 'PASSED', violations: { proximity: [], velocity: [], acceleration: [], yaw_rate: [] } },
    });
    expect(html).not.toContain('preview-export-notice');
  });

  it('does not offer a preview export when there is no jerk evidence at all', () => {
    const html = render({
      hasShow: true,
      validation: {
        status: 'PASSED',
        feasibility_certified: false,
        violations: { proximity: [], velocity: [], acceleration: [], yaw_rate: [] },
      },
    });
    expect(html).not.toContain('preview-export-notice');
  });

  it('does not offer a preview export when there is no built show', () => {
    const html = render({ hasShow: false, validation: null });
    expect(html).not.toContain('preview-export-notice');
  });

  it('still offers a preview export when the only extra violations are altitude or ground', () => {
    const html = render({
      hasShow: true,
      validation: {
        status: 'FAILED',
        feasibility_certified: false,
        violations: {
          proximity: [], velocity: [], acceleration: [], yaw_rate: [],
          jerk: [{ time: 1.5, drone: 0, jerk: 35.0, limit: 20.0 }],
          altitude: [{ time: 2.0, drone: 0, altitude: 150.0, limit: 120.0 }],
        },
      },
    });
    // An altitude (or ground) breach is a hard limit preview_only still
    // enforces server-side, so it must count the same as proximity/velocity
    // in deciding whether jerk is really the *sole* reason export is refused.
    expect(html).not.toContain('preview-export-notice');
  });

  it('formats the preview-export success message per format (issue #161 review)', () => {
    expect(previewExportSuccessMessage('json')).toBe('Preview exported (json) — unsigned, not flight-certified.');
    expect(previewExportSuccessMessage('tara')).toBe('Preview exported (tara) — unsigned, not flight-certified.');
  });
});
