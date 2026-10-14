import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import TransmitPanel from './TransmitPanel.jsx';

const noop = () => {};

const render = (props = {}) => renderToStaticMarkup(
  <TransmitPanel
    homeLat={12.9716}
    homeLon={77.5946}
    onHomeLat={noop}
    onHomeLon={noop}
    onExport={noop}
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
