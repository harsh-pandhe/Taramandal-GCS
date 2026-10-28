import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import SafetyPanel from './SafetyPanel.jsx';
import liveResponse from '../../engine/__fixtures__/validation-response.json';

// Rendered to static markup: the panel is pure presentation, so this checks
// exactly what an operator would see without needing a DOM environment.
const render = (validation) => renderToStaticMarkup(<SafetyPanel validation={validation} />);
const statuses = (html) => [...html.matchAll(/data-metric-status="(\w+)"/g)].map((m) => m[1]);

describe('SafetyPanel', () => {
  it('judges a live /api/validate response by the limits it carries', () => {
    // The fixture is two drones 1.8m apart: failed against the 2.0m design
    // limit, above the 1.5m abort. The old panel showed this green.
    // Altitude (5.0m, issue #148) is the 5th card, well under the 120m
    // ceiling. /api/validate now returns the same certify_feasibility_report
    // /api/generate and /api/export use (issue #151/#165), so the fixture
    // always carries a judged jerk metric too - a stationary two-waypoint
    // hover has zero jerk, well under the 20 m/s³ limit.
    const html = render(liveResponse);
    expect(statuses(html)).toEqual(['warning', 'passed', 'passed', 'passed', 'passed', 'passed']);
    const { limits } = liveResponse;
    expect(html).toContain(`≥ ${limits.design_separation_m}m · abort &lt; ${limits.min_separation_m}m`);
    expect(html).toContain(`≤ ${limits.max_velocity_ms} m/s`);
    expect(html).toContain(`≤ ${limits.max_accel_ms2} m/s²`);
    expect(html).toContain(`≤ ${limits.max_yaw_rate_dps} °/s`);
    expect(html).toContain(`≤ ${limits.max_altitude_m} m`);
    expect(html).toContain(`≤ ${limits.max_jerk_ms3} m/s³`);
  });

  it('reflects intentionally mismatched limits instead of built-in thresholds', () => {
    const html = render({
      ...liveResponse,
      limits: { ...liveResponse.limits, min_separation_m: 0.5, design_separation_m: 1.0, max_velocity_ms: 0.0 },
    });
    // 1.8m now clears a 1.0m design limit; 0 m/s still meets a 0 m/s ceiling.
    expect(statuses(html)).toEqual(['passed', 'passed', 'passed', 'passed', 'passed', 'passed']);
    expect(html).toContain('≥ 1m · abort &lt; 0.5m');
  });

  it('shows metrics unjudged when the report has no limits', () => {
    const { limits: _omit, ...legacy } = liveResponse;
    const html = render(legacy);
    expect(statuses(html)).toEqual(['unknown', 'unknown', 'unknown', 'unknown', 'unknown', 'unknown']);
    expect(html).toContain('limit unknown');
    expect(html).not.toMatch(/metric-val (passed|warning|danger)/);
  });

  it('shows a real altitude breach as a danger card and a violation row (issue #148)', () => {
    const html = render({
      ...liveResponse,
      status: 'FAILED',
      metrics: { ...liveResponse.metrics, max_altitude_m: 150.0 },
      violations: {
        ...liveResponse.violations,
        altitude: [{ time: 0.0, drone: 0, altitude: 150.0, limit: 120.0 }],
      },
    });
    expect(statuses(html)).toEqual(['warning', 'passed', 'passed', 'passed', 'danger', 'passed']);
    expect(html).toContain('Altitude · Drone 0');
    expect(html).toContain('150m @ 0s');
  });

  it('shows a ground-penetration violation as its own category, not folded into altitude', () => {
    const html = render({
      ...liveResponse,
      status: 'FAILED',
      violations: {
        ...liveResponse.violations,
        ground: [{ time: 0.0, drone: 0, altitude: -2.0, limit: 0.0 }],
      },
    });
    expect(html).toContain('Below ground · Drone 0');
    expect(html).toContain('-2m @ 0s');
  });

  it('omits the altitude card for a report from before the backend sent it, without crashing', () => {
    const { max_altitude_m: _m1, min_altitude_m: _m2, ...legacyMetrics } = liveResponse.metrics;
    const { altitude: _a, ground: _g, ...legacyViolations } = liveResponse.violations;
    const html = render({ ...liveResponse, metrics: legacyMetrics, violations: legacyViolations });
    // Jerk's card still renders (issue #151's "always show" treatment is
    // independent of altitude's "omit when absent" one, issue #148) - and
    // the fixture carries a judged jerk metric regardless of altitude.
    expect(statuses(html)).toEqual(['warning', 'passed', 'passed', 'passed', 'passed']);
    expect(html).not.toContain('Max Altitude');
  });

  it('renders yaw-rate violations and counts them in the total (issue #151)', () => {
    const html = render({
      ...liveResponse,
      violations: {
        ...liveResponse.violations,
        yaw_rate: [{ time: 1.5, drone: 2, yaw_rate: 310.4 }],
      },
    });
    expect(html).toContain('Violations (4)');
    expect(html).toContain('Yaw rate · Drone 2');
    expect(html).toContain('310.4°/s @ 1.5s');
  });

  it('judges the jerk metric when a response actually carries it (e.g. /api/generate)', () => {
    const withJerk = {
      ...liveResponse,
      metrics: { ...liveResponse.metrics, max_jerk_m_s3: 25.3 },
      violations: { ...liveResponse.violations, jerk: [{ time: 0.4, drone: 1, jerk: 25.3 }] },
    };
    const html = render(withJerk);
    expect(statuses(html)).toEqual(['warning', 'passed', 'passed', 'passed', 'passed', 'danger']);
    expect(html).toContain('≤ 20 m/s³');
    expect(html).toContain('Jerk · Drone 1');
    expect(html).toContain('25.3m/s³ @ 0.4s');
  });

  it('renders and counts every violation category the backend can send (issue #151 review)', () => {
    // One of each of the 7 categories certify_feasibility_report can return.
    // A category added to the backend without a matching row+count here
    // would silently vanish from the panel instead of failing this test.
    const oneOfEach = {
      proximity: [{ time: 0.0, drones: [0, 1], distance: 1.8, limit: 2.0 }],
      velocity: [{ time: 0.1, drone: 0, velocity: 9.0 }],
      acceleration: [{ time: 0.2, drone: 1, acceleration: 60.0 }],
      yaw_rate: [{ time: 0.3, drone: 2, yaw_rate: 310.4 }],
      jerk: [{ time: 0.4, drone: 1, jerk: 25.3 }],
      altitude: [{ time: 0.5, drone: 0, altitude: 150.0 }],
      ground: [{ time: 0.6, drone: 0, altitude: -2.0 }],
    };
    expect(Object.keys(oneOfEach).sort()).toEqual(
      ['acceleration', 'altitude', 'ground', 'jerk', 'proximity', 'velocity', 'yaw_rate'],
    );
    const html = render({
      ...liveResponse,
      metrics: { ...liveResponse.metrics, max_jerk_m_s3: 25.3 },
      violations: oneOfEach,
    });
    expect(html).toContain('Violations (7)');
    expect(html).toContain('Proximity breach');
    expect(html).toContain('Speed · Drone 0');
    expect(html).toContain('Accel · Drone 1');
    expect(html).toContain('Yaw rate · Drone 2');
    expect(html).toContain('Jerk · Drone 1');
    expect(html).toContain('Altitude · Drone 0');
    expect(html).toContain('Below ground · Drone 0');
  });

  it('shows a validating indicator while a background validation is in flight (issue #173)', () => {
    // No `validation` yet (it's still running) but a `choreoData` already
    // exists and `validating` is true - without this, the panel looked
    // identical to "never validated at all", which at large fleet sizes
    // (~15s at 1000 drones) left an operator with no feedback for a long,
    // silent wait.
    const html = renderToStaticMarkup(
      <SafetyPanel validation={null} validating choreoData={{ drones: [] }} />,
    );
    expect(html).toContain('Validating');
    expect(html).toMatch(/validation-summary unverified/);
  });

  it('does not show the validating indicator once validation has actually run', () => {
    const html = render(liveResponse);
    expect(html).not.toContain('Validating —');
  });

  it('does not show the validating indicator before any build has happened', () => {
    const html = renderToStaticMarkup(<SafetyPanel validation={null} validating choreoData={null} />);
    expect(html).not.toContain('Validating —');
    expect(html).toContain('No validation yet');
  });
});
