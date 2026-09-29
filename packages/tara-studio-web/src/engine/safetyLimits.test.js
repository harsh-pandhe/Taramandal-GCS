import { describe, expect, it } from 'vitest';
import { judgeMetrics } from './safetyLimits.js';
import liveResponse from './__fixtures__/validation-response.json';

// `liveResponse` is a real /api/validate response, pinned to the backend by
// packages/tara-studio/tests/test_safety_limits_contract.py. Its limits are
// tara_core's DEFAULT_LIMITS as they actually are, not a copy typed in here.
const LIVE = liveResponse.limits;

const metrics = (over = {}) => ({
  min_distance_m: 10,
  max_velocity_m_s: 0,
  max_acceleration_m_s2: 0,
  max_yaw_rate_deg_s: 0,
  ...over,
});

const statusOf = (cards, key) => cards.find((c) => c.key === key).status;

describe('judgeMetrics against the live backend limits', () => {
  it('bands separation by the design limit and the runtime abort', () => {
    const at = (d) => statusOf(judgeMetrics(metrics({ min_distance_m: d }), LIVE), 'separation');
    expect(at(LIVE.design_separation_m)).toBe('passed');
    expect(at(LIVE.design_separation_m - 0.01)).toBe('warning');
    expect(at(LIVE.min_separation_m)).toBe('warning');
    expect(at(LIVE.min_separation_m - 0.01)).toBe('danger');
  });

  it('never shows green between the old 0.8m threshold and the runtime abort', () => {
    // The drift this issue exists for: 0.8-1.5m was green, and the fleet aborts there.
    expect(statusOf(judgeMetrics(metrics({ min_distance_m: 1.0 }), LIVE), 'separation')).toBe('danger');
  });

  it('passes speed, acceleration and yaw up to and including their ceilings', () => {
    const cards = judgeMetrics(metrics({
      max_velocity_m_s: LIVE.max_velocity_ms,
      max_acceleration_m_s2: LIVE.max_accel_ms2,
      max_yaw_rate_deg_s: LIVE.max_yaw_rate_dps,
    }), LIVE);
    expect(statusOf(cards, 'velocity')).toBe('passed');
    expect(statusOf(cards, 'acceleration')).toBe('passed');
    expect(statusOf(cards, 'yaw')).toBe('passed');
  });

  it('no longer warns on legal shows the old thresholds flagged', () => {
    // 5 m/s, 10 m/s² and 250°/s were all "warning" under 4.0 / 2.0 / 180.
    const cards = judgeMetrics(metrics({ max_velocity_m_s: 5, max_acceleration_m_s2: 10, max_yaw_rate_deg_s: 250 }), LIVE);
    expect(cards.map((c) => c.status)).toEqual(['passed', 'passed', 'passed', 'passed']);
  });

  it('fails each ceiling just above it', () => {
    const cards = judgeMetrics(metrics({
      max_velocity_m_s: LIVE.max_velocity_ms + 0.01,
      max_acceleration_m_s2: LIVE.max_accel_ms2 + 0.01,
      max_yaw_rate_deg_s: LIVE.max_yaw_rate_dps + 1,
    }), LIVE);
    expect(['velocity', 'acceleration', 'yaw'].map((k) => statusOf(cards, k))).toEqual(['danger', 'danger', 'danger']);
  });
});

describe('judgeMetrics follows whatever limits it is given', () => {
  // Deliberately unlike the real limits: if any threshold were a literal in
  // the code, one of these would come out the other way.
  const MISMATCHED = {
    min_separation_m: 5, design_separation_m: 8, max_velocity_ms: 1, max_accel_ms2: 0.5, max_yaw_rate_dps: 10,
  };

  it('judges by the supplied numbers, not built-in ones', () => {
    const cards = judgeMetrics(metrics({ min_distance_m: 6, max_velocity_m_s: 2, max_acceleration_m_s2: 1, max_yaw_rate_deg_s: 20 }), MISMATCHED);
    expect(cards.map((c) => c.status)).toEqual(['warning', 'danger', 'danger', 'danger']);
  });

  it('labels each card with the supplied limit', () => {
    const labels = judgeMetrics(metrics(), MISMATCHED).map((c) => c.limitLabel);
    expect(labels).toEqual(['≥ 8m · abort < 5m', '≤ 1 m/s', '≤ 0.5 m/s²', '≤ 10 °/s']);
  });

  it('refuses to judge without limits rather than guessing', () => {
    for (const limits of [undefined, null, {}]) {
      const cards = judgeMetrics(metrics(), limits);
      expect(cards.map((c) => c.status)).toEqual(['unknown', 'unknown', 'unknown', 'unknown']);
      expect(cards.every((c) => c.limitLabel === 'limit unknown')).toBe(true);
    }
  });
});
