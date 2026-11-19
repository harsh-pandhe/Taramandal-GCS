import { useState, useEffect } from 'react';
import { API_BASE } from '../config';
import { gcsFetch } from '../session';

/**
 * The fleet discovery registry (issues #58, #304), polled from GET /api/fleet/registry every 2 s.
 *
 * `registry` is null while discovery is off (`{"enabled": false}`), the GCS is older than the endpoint, or nothing has
 * answered yet: the dashboard then shows nothing about it. `stale` turns true after two missed polls so a frozen readout
 * is not mistaken for a live one.
 */
export default function useFleetRegistry() {
  const [state, setState] = useState({ registry: null, stale: false });

  useEffect(() => {
    let alive = true;
    let misses = 0;
    const poll = async () => {
      try {
        const res = await gcsFetch(`${API_BASE}/api/fleet/registry`);
        const data = res.ok ? await res.json() : null;
        if (!alive) return;
        misses = 0;
        setState({ registry: data && data.enabled ? data : null, stale: false });
      } catch {
        if (!alive) return;
        misses += 1;
        if (misses >= 2) setState((s) => (s.registry ? { ...s, stale: true } : s));
      }
    };
    poll();
    const id = setInterval(poll, 2000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  return state;
}
