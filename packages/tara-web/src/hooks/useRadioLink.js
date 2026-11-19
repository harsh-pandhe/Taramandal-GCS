import { useState, useEffect } from 'react';
import { API_BASE } from '../config';
import { gcsFetch } from '../session';

/**
 * The serial radio's link health (issue #68), polled from GET /api/radio-link every 2 s.
 *
 * Returns null while the GCS has no radio configured, has not answered yet, or cannot be reached: the dashboard then shows
 * nothing, never a stale or invented reading. `stale` turns true after two missed polls so a frozen readout is not mistaken
 * for a live one.
 */
export default function useRadioLink() {
  const [state, setState] = useState({ link: null, stale: false });

  useEffect(() => {
    let alive = true;
    let misses = 0;
    const poll = async () => {
      try {
        const res = await gcsFetch(`${API_BASE}/api/radio-link`);
        const data = res.ok ? await res.json() : null;
        if (!alive) return;
        if (data && data.configured) {
          misses = 0;
          setState({ link: data, stale: false });
        } else {
          setState({ link: null, stale: false });
        }
      } catch {
        if (!alive) return;
        misses += 1;
        if (misses >= 2) setState((s) => (s.link ? { ...s, stale: true } : s));
      }
    };
    poll();
    const id = setInterval(poll, 2000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  return state;
}
