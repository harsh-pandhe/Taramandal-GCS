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
    // Altitude (5.0m, issue #148) is the 5th card, well under the 120m ceiling.
    const html = render(liveResponse);
    expect(statuses(html)).toEqual(['warning', 'passed', 'passed', 'passed', 'passed']);
    const { limits } = liveResponse;
    expect(html).toContain(`≥ ${limits.design_separation_m}m · abort &lt; ${limits.min_separation_m}m`);
    expect(html).toContain(`≤ ${limits.max_velocity_ms} m/s`);
    expect(html).toContain(`≤ ${limits.max_accel_ms2} m/s²`);
    expect(html).toContain(`≤ ${limits.max_yaw_rate_dps} °/s`);
    expect(html).toContain(`≤ ${limits.max_altitude_m} m`);
  });

  it('reflects intentionally mismatched limits instead of built-in thresholds', () => {
    const html = render({
      ...liveResponse,
      limits: { ...liveResponse.limits, min_separation_m: 0.5, design_separation_m: 1.0, max_velocity_ms: 0.0 },
    });
    // 1.8m now clears a 1.0m design limit; 0 m/s still meets a 0 m/s ceiling.
    expect(statuses(html)).toEqual(['passed', 'passed', 'passed', 'passed', 'passed']);
    expect(html).toContain('≥ 1m · abort &lt; 0.5m');
  });

  it('shows metrics unjudged when the report has no limits', () => {
    const { limits: _omit, ...legacy } = liveResponse;
    const html = render(legacy);
    expect(statuses(html)).toEqual(['unknown', 'unknown', 'unknown', 'unknown', 'unknown']);
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
    expect(statuses(html)).toEqual(['warning', 'passed', 'passed', 'passed', 'danger']);
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
    expect(statuses(html)).toEqual(['warning', 'passed', 'passed', 'passed']);
    expect(html).not.toContain('Max Altitude');
  });
});
