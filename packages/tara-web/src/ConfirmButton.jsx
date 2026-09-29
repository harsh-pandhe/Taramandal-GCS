import { useEffect, useRef } from 'react';

// How long an armed emergency button waits for its confirming click before
// falling back to idle. Long enough to move a hand; short enough that a
// forgotten armed button can't be confirmed by a stray click minutes later.
export const CONFIRM_WINDOW_MS = 5000;

// Clicks this soon after arming are ignored (not treated as a disarm either):
// a reflexive double-click must not count as a deliberate confirmation.
export const CONFIRM_GUARD_MS = 350;

/**
 * An emergency action that needs one confirming click (issue #98).
 *
 * The first click arms it and the label turns into a question with a
 * countdown; a second click inside CONFIRM_WINDOW_MS fires `onConfirm`.
 * Escape or the timeout disarms. Deliberately a single extra click rather
 * than a modal or a form, so it costs a genuine emergency about a second.
 *
 * Controlled: the parent owns which action (if any) is armed, so arming one
 * emergency button disarms the others and a confirm can't land on the wrong
 * action. This is operator-error protection only. The backend's own
 * interlocks remain the safety gate.
 */
export default function ConfirmButton({
  className, label, confirmLabel, armed, onArm, onDisarm, onConfirm,
}) {
  // When the arming click happened, from the event's own timestamp. Handlers
  // and effects can run late on a busy main thread (the 3D views compiling
  // shaders, say); the interval that matters is between the operator's two
  // clicks, not between two moments the browser got round to running code.
  const armedAt = useRef(0);
  // The dashboard re-renders at telemetry rate (~20Hz), usually with a fresh
  // onDisarm closure. Depending on it directly would restart the timeout
  // every frame, so the effect keys on `armed` alone.
  const disarmRef = useRef(onDisarm);
  useEffect(() => { disarmRef.current = onDisarm; });

  useEffect(() => {
    if (!armed) return undefined;
    const remaining = Math.max(0, CONFIRM_WINDOW_MS - (performance.now() - armedAt.current));
    const timer = setTimeout(() => disarmRef.current(), remaining);
    const onKey = (e) => { if (e.key === 'Escape') disarmRef.current(); };
    window.addEventListener('keydown', onKey);
    return () => {
      clearTimeout(timer);
      window.removeEventListener('keydown', onKey);
    };
  }, [armed]);

  const handleClick = (e) => {
    const sinceArmed = e.timeStamp - armedAt.current;
    if (!armed || sinceArmed > CONFIRM_WINDOW_MS) {
      // Idle, or a confirm that came after the window even if the timeout
      // hadn't fired yet: (re-)arm rather than act on a stale question.
      armedAt.current = e.timeStamp;
      onArm();
      return;
    }
    if (sinceArmed < CONFIRM_GUARD_MS) return;
    onDisarm();
    onConfirm();
  };

  return (
    <button
      type="button"
      className={`btn ${className}${armed ? ' btn-armed' : ''}`}
      onClick={handleClick}
      aria-pressed={armed}
      data-armed={armed ? 'true' : 'false'}
      style={armed ? { '--confirm-window': `${CONFIRM_WINDOW_MS}ms` } : undefined}
    >
      {armed ? confirmLabel : label}
      {armed && <span className="confirm-countdown" aria-hidden="true" />}
      <span className="sr-only" aria-live="assertive">
        {armed ? `${label} armed. Click again within ${CONFIRM_WINDOW_MS / 1000} seconds to confirm, or press Escape to cancel.` : ''}
      </span>
    </button>
  );
}
