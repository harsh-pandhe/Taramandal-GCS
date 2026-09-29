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
    const html = render(liveResponse);
    expect(statuses(html)).toEqual(['warning', 'passed', 'passed', 'passed']);
    const { limits } = liveResponse;
    expect(html).toContain(`≥ ${limits.design_separation_m}m · abort &lt; ${limits.min_separation_m}m`);
    expect(html).toContain(`≤ ${limits.max_velocity_ms} m/s`);
    expect(html).toContain(`≤ ${limits.max_accel_ms2} m/s²`);
    expect(html).toContain(`≤ ${limits.max_yaw_rate_dps} °/s`);
  });

  it('reflects intentionally mismatched limits instead of built-in thresholds', () => {
    const html = render({
      ...liveResponse,
      limits: { ...liveResponse.limits, min_separation_m: 0.5, design_separation_m: 1.0, max_velocity_ms: 0.0 },
    });
    // 1.8m now clears a 1.0m design limit; 0 m/s still meets a 0 m/s ceiling.
    expect(statuses(html)).toEqual(['passed', 'passed', 'passed', 'passed']);
    expect(html).toContain('≥ 1m · abort &lt; 0.5m');
  });

  it('shows metrics unjudged when the report has no limits', () => {
    const { limits: _omit, ...legacy } = liveResponse;
    const html = render(legacy);
    expect(statuses(html)).toEqual(['unknown', 'unknown', 'unknown', 'unknown']);
    expect(html).toContain('limit unknown');
    expect(html).not.toMatch(/metric-val (passed|warning|danger)/);
  });
});
