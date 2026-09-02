import { useRef, useEffect, useCallback } from 'react';
import { drawWaveform } from '../../engine/audioEngine.js';

/**
 * Audio waveform lane drawn behind the timeline.
 *
 * Shares the timeline's coordinate system: the visible window is
 * [scrollStart, scrollStart + duration/zoom], so the waveform, the scene
 * blocks and the playhead all agree on where a given second sits. Clicking or
 * dragging scrubs, same as the scene lane.
 */
export default function WaveformTrack({
  peaks, duration, currentTime, zoom = 1, scrollStart = 0, onScrub,
}) {
  const canvasRef = useRef(null);
  const wrapRef = useRef(null);
  const draggingRef = useRef(false);

  const windowSpan = duration / Math.max(1, zoom);
  const startTime = Math.max(0, Math.min(scrollStart, Math.max(0, duration - windowSpan)));
  const endTime = startTime + windowSpan;

  const redraw = useCallback(() => {
    drawWaveform(canvasRef.current, peaks, {
      duration, startTime, endTime,
      color: 'rgba(56,189,248,0.55)',
      baseline: 'rgba(148,163,184,0.25)',
    });
  }, [peaks, duration, startTime, endTime]);

  useEffect(() => { redraw(); }, [redraw]);

  // The canvas is sized from its CSS box, so it has to be repainted whenever
  // that box changes — otherwise the trace stretches on a window resize.
  useEffect(() => {
    const el = wrapRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver(redraw);
    ro.observe(el);
    return () => ro.disconnect();
  }, [redraw]);

  const timeFromEvent = useCallback((clientX) => {
    const el = wrapRef.current;
    if (!el || duration <= 0) return 0;
    const rect = el.getBoundingClientRect();
    const ratio = (clientX - rect.left) / rect.width;
    return Math.max(0, Math.min(duration, startTime + ratio * windowSpan));
  }, [duration, startTime, windowSpan]);

  useEffect(() => {
    if (!onScrub) return undefined;
    const move = (e) => { if (draggingRef.current) onScrub(timeFromEvent(e.clientX)); };
    const up = () => { draggingRef.current = false; };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    return () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
  }, [timeFromEvent, onScrub]);

  if (!peaks) return null;

  // Playhead position within the visible window; hidden when scrolled away.
  const playPct = ((currentTime - startTime) / windowSpan) * 100;
  const playVisible = playPct >= 0 && playPct <= 100;

  return (
    <div
      className="tl-lane tl-lane-wave"
      ref={wrapRef}
      onPointerDown={(e) => {
        if (!onScrub) return;
        draggingRef.current = true;
        onScrub(timeFromEvent(e.clientX));
      }}
    >
      <canvas ref={canvasRef} className="tl-wave-canvas" />
      {playVisible && (
        <div className="tl-playhead tl-playhead-wave" style={{ left: `${playPct}%` }} />
      )}
    </div>
  );
}
