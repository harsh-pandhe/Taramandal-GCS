import { useState, useRef, useEffect, useCallback } from 'react';

/**
 * Show playback clock.
 *
 * Drives `currentTime` from a rAF loop while playing. The loop reads speed and
 * loop settings through refs so changing them mid-playback doesn't restart the
 * effect (which would reset the frame delta and stutter the preview).
 */
export default function usePlayback(duration) {
  const [currentTime, setCurrentTime] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [isLooping, setIsLooping] = useState(true);

  const speedRef = useRef(speed);
  const loopRef = useRef(isLooping);
  const durationRef = useRef(duration);
  speedRef.current = speed;
  loopRef.current = isLooping;
  durationRef.current = duration;

  useEffect(() => {
    if (!isPlaying || duration <= 0) return undefined;

    let last = performance.now();
    let frame;
    const tick = () => {
      const now = performance.now();
      const delta = (now - last) / 1000;
      last = now;

      setCurrentTime((prev) => {
        const next = prev + delta * speedRef.current;
        const end = durationRef.current;
        if (next >= end) {
          if (loopRef.current) return next - end; // carry the remainder, no hitch
          setIsPlaying(false);
          return end;
        }
        return next;
      });
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [isPlaying, duration]);

  // A shortened show must not leave the playhead stranded past the end.
  useEffect(() => {
    setCurrentTime((t) => Math.min(t, duration));
  }, [duration]);

  const scrub = useCallback((t) => {
    setCurrentTime(Math.max(0, Math.min(durationRef.current, t)));
  }, []);

  const rewind = useCallback(() => setCurrentTime(0), []);
  const togglePlay = useCallback(() => setIsPlaying((p) => !p), []);
  const toggleLoop = useCallback(() => setIsLooping((l) => !l), []);

  return {
    currentTime, isPlaying, speed, isLooping,
    setSpeed, scrub, rewind, togglePlay, toggleLoop, setIsPlaying,
  };
}
