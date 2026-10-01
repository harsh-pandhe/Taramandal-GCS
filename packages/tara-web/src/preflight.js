import { useEffect, useState } from 'react';

// Data side of the pre-arm checklist HUD (issue #48): polling the GCS verdict
// and rolling per-drone gates up into one card per rule. Components live in
// PreflightChecklist.jsx.

const POLL_MS = 1000;

// Worst state first: the card for a rule shows the worst state of any drone.
const STATE_RANK = ['fail', 'overridden', 'warn', 'pass', 'ignored'];
export const STATE_LABEL = {
  pass: '✓ PASS',
  fail: '✗ BLOCKED',
  warn: '! WARN',
  overridden: '⚠ OVERRIDDEN',
  ignored: '— N/A',
};

export function usePreflight(apiBase, pollMs = POLL_MS) {
  const [report, setReport] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await fetch(`${apiBase}/api/preflight-check`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!cancelled) {
          setReport(data);
          setError('');
        }
      } catch (err) {
        // An unreachable GCS must read as "not cleared", never as the last
        // green report we happened to receive.
        if (!cancelled) {
          setReport(null);
          setError(err.message);
        }
      }
    };
    poll();
    const timer = setInterval(poll, pollMs);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [apiBase, pollMs]);

  return { report, error };
}

// One card per rule: fleet rules carry their own gate; per-drone rules are
// rolled up across the fleet so the HUD stays readable at hundreds of drones.
export function summarizeRules(report) {
  if (!report) return [];
  const drones = Object.entries(report.drones || {});
  return report.rules.map((rule) => {
    // An ignored rule's pass/fail is meaningless under this profile; showing
    // "0/5 pass" for it would read as a failure the operator should chase.
    if (rule.severity === 'IGNORED') {
      return { ...rule, state: 'ignored', detail: `Not checked under ${report.profile}`, failures: [] };
    }
    if (rule.scope === 'fleet') {
      const g = report.fleet_gates.find((x) => x.rule === rule.id);
      return { ...rule, state: g?.state ?? 'ignored', detail: g?.detail ?? '', failures: [] };
    }
    const gates = drones.map(([id, d]) => ({ id, ...d.gates.find((x) => x.rule === rule.id) }));
    const state = STATE_RANK.find((s) => gates.some((g) => g.state === s)) ?? 'ignored';
    const passing = gates.filter((g) => g.passed).length;
    const failures = gates.filter((g) => !g.passed && g.state !== 'ignored');
    return {
      ...rule,
      state,
      detail: gates.length ? `${passing}/${gates.length} drones pass` : 'No drones reporting',
      failures,
    };
  });
}
