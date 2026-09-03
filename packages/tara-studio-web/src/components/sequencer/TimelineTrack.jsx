import { useRef, useCallback, useEffect } from 'react';

// Chooses a tick spacing that keeps markers readable at any show length.
const tickStep = (duration) => {
  const targets = [0.5, 1, 2, 5, 10, 15, 30, 60, 120];
  return targets.find((t) => duration / t <= 12) ?? 300;
};

const fmtTick = (t) => (t >= 60 ? `${Math.floor(t / 60)}:${String(Math.round(t % 60)).padStart(2, '0')}` : `${t}s`);

/**
 * Scrubbable multi-layer timeline.
 *
 * Layer 1 draws the show's legs (takeoff / morph / hold / land) as blocks
 * against real time; layer 2 is the time ruler. Dragging anywhere on the track
 * scrubs, and the playhead reflects `currentTime`.
 *
 * Scrubbing is pointer-event based rather than an <input type=range> so the
 * blocks stay clickable — clicking a scene block jumps to it.
 */
export default function TimelineTrack({
  segments, duration, currentTime, onScrub, onSelectScene, activeSceneId,
}) {
  const trackRef = useRef(null);
  const draggingRef = useRef(false);

  const timeFromEvent = useCallback((clientX) => {
    const el = trackRef.current;
    if (!el || duration <= 0) return 0;
    const rect = el.getBoundingClientRect();
    const ratio = (clientX - rect.left) / rect.width;
    return Math.max(0, Math.min(duration, ratio * duration));
  }, [duration]);

  // Pointer move/up live on window so a drag that leaves the track keeps
  // scrubbing and always releases, instead of sticking when the pointer is
  // let go outside the element.
  useEffect(() => {
    const move = (e) => { if (draggingRef.current) onScrub(timeFromEvent(e.clientX)); };
    const up = () => { draggingRef.current = false; };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    return () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
  }, [timeFromEvent, onScrub]);

  const pct = (t) => (duration > 0 ? (t / duration) * 100 : 0);
  const step = tickStep(duration || 1);
  const ticks = [];
  for (let t = 0; t <= duration + 1e-9; t += step) ticks.push(parseFloat(t.toFixed(3)));

  return (
    <div className="timeline-track">
      <div
        className="tl-lane tl-lane-scenes"
        ref={trackRef}
        onPointerDown={(e) => {
          draggingRef.current = true;
          onScrub(timeFromEvent(e.clientX));
        }}
        role="slider"
        tabIndex={0}
        aria-label="Show timeline"
        aria-valuemin={0}
        aria-valuemax={duration}
        aria-valuenow={currentTime}
        aria-valuetext={`${currentTime.toFixed(2)} of ${duration.toFixed(2)} seconds`}
        onKeyDown={(e) => {
          const nudge = e.shiftKey ? 1 : 0.1;
          if (e.key === 'ArrowLeft') { e.preventDefault(); onScrub(Math.max(0, currentTime - nudge)); }
          if (e.key === 'ArrowRight') { e.preventDefault(); onScrub(Math.min(duration, currentTime + nudge)); }
          if (e.key === 'Home') { e.preventDefault(); onScrub(0); }
          if (e.key === 'End') { e.preventDefault(); onScrub(duration); }
        }}
      >
        {segments.map((seg, i) => (
          <div
            key={`${seg.kind}-${i}`}
            className={`tl-seg tl-${seg.kind}${seg.sceneId && seg.sceneId === activeSceneId ? ' active' : ''}`}
            style={{ left: `${pct(seg.start)}%`, width: `${pct(seg.end - seg.start)}%` }}
            title={`${seg.label} · ${seg.start.toFixed(1)}s – ${seg.end.toFixed(1)}s`}
            onPointerDown={(e) => {
              // Selecting a scene shouldn't fight the scrub gesture; let the
              // lane handle the scrub and just record the selection.
              if (seg.sceneId && onSelectScene) onSelectScene(seg.sceneId);
              e.stopPropagation();
              draggingRef.current = true;
              onScrub(timeFromEvent(e.clientX));
            }}
          >
            <span className="tl-seg-label">{seg.label}</span>
          </div>
        ))}

        <div className="tl-playhead" style={{ left: `${pct(currentTime)}%` }}>
          <span className="tl-playhead-time">{currentTime.toFixed(2)}s</span>
        </div>
      </div>

      <div className="tl-lane tl-lane-ruler">
        {ticks.map((t) => (
          <span key={t} className="tl-tick" style={{ left: `${pct(t)}%` }}>
            <i />{fmtTick(t)}
          </span>
        ))}
      </div>
    </div>
  );
}
