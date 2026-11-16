import { useEffect, useRef, useState } from 'react';
import ConfirmButton from '../../ConfirmButton';
import { hoverAvailability, hoverSteps } from '../../hoverSequence';

const TARGET_ALT_M = 1.0;
const HOVER_S = 5.0;

/**
 * The supervised single-drone hover and slow landing (#171), started by a human in the dashboard: one click arms it,
 * a second click inside the confirm window sends it. The GCS does all the gating (role, mode, pre-arm gates, fleet
 * state, one sequence at a time); this only asks, waits, and shows the GCS's own answer or refusal. It is shown for
 * simulation only: on hardware the endpoint stays behind the #135 gate.
 */
export default function HoverTestPanel({ mode, modeLoaded, role, connectedCount, run }) {
  const [armed, setArmed] = useState(false);
  const [state, setState] = useState({ phase: 'idle' }); // idle | running | done | failed
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  if (!modeLoaded) return null;
  const running = state.phase === 'running';
  const { ok, reason } = hoverAvailability({ mode, role, connectedCount, running });

  const start = async () => {
    setState({ phase: 'running' });
    try {
      const body = await run(TARGET_ALT_M, HOVER_S);
      if (alive.current) setState({ phase: 'done', status: String(body?.status ?? ''), steps: hoverSteps(body) });
    } catch (e) {
      if (alive.current) setState({ phase: 'failed', message: e?.message || 'The hover sequence failed.' });
    }
  };

  return (
    <div className="hover-test" data-testid="hover-test">
      <div className="hover-test-head">
        <span>HOVER TEST</span>
        <span className="hover-test-sub">drone 0 · {TARGET_ALT_M} m · {HOVER_S} s · slow landing</span>
      </div>
      {ok ? (
        <ConfirmButton
          className="btn-launch"
          label={`HOVER TEST ${TARGET_ALT_M} m`}
          confirmLabel="CONFIRM: ARM AND CLIMB?"
          armed={armed}
          onArm={() => setArmed(true)}
          onDisarm={() => setArmed(false)}
          onConfirm={start}
        />
      ) : (
        <button type="button" className="btn btn-launch" disabled data-testid="hover-test-disabled">
          {running ? 'HOVER SEQUENCE RUNNING…' : `HOVER TEST ${TARGET_ALT_M} m`}
        </button>
      )}
      {!ok && !running && <p className="hover-test-note" data-testid="hover-test-reason">{reason}</p>}
      {running && <p className="hover-test-note" role="status" data-testid="hover-test-running">The drone is arming, climbing and landing. Emergency LAND and E-STOP stay available above.</p>}
      {state.phase === 'done' && (
        <div className={`hover-test-result ${state.status === 'COMPLETED' ? 'ok' : 'bad'}`} role="status" data-testid="hover-test-result">
          <strong>{state.status || 'FINISHED'}</strong>
          <ol>
            {state.steps.map((s) => (
              <li key={s.key}>{s.label}: {s.status}{s.detail ? ` (${s.detail})` : ''}</li>
            ))}
          </ol>
        </div>
      )}
      {state.phase === 'failed' && (
        <div className="hover-test-result bad" role="alert" data-testid="hover-test-error">{state.message}</div>
      )}
    </div>
  );
}
