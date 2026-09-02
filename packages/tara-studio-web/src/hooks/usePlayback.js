import { useState, useRef, useEffect, useCallback } from 'react';

/**
 * Show playback clock.
 *
 * Drives `currentTime` from a rAF loop while playing. The loop reads speed and
 * loop settings through refs so changing them mid-playback doesn't restart the
 * effect (which would reset the frame delta and stutter the preview).
 *
 * When an `audio` transport is supplied (issue #14), that becomes the master
 * clock: each frame reads the AudioContext's own time instead of accumulating
 * deltas. A JS accumulator and the audio hardware clock drift apart over a few
 * minutes, which on a show cut to music means the lights fall out of sync with
 * the soundtrack. rAF still paces the redraw; it just no longer defines time.
 *
 * @param {number} duration show length (s)
 * @param {object} [audio]  optional AudioClock-like transport
 */
export default function usePlayback(duration, audio = null) {
  const [currentTime, setCurrentTime] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [isLooping, setIsLooping] = useState(true);

  const speedRef = useRef(speed);
  const loopRef = useRef(isLooping);
  const durationRef = useRef(duration);
  const audioRef = useRef(audio);
  const currentTimeRef = useRef(currentTime);
  speedRef.current = speed;
  loopRef.current = isLooping;
  durationRef.current = duration;
  audioRef.current = audio;
  currentTimeRef.current = currentTime;

  // Audio drives the clock only at 1x: a BufferSource plays at its natural
  // rate here, so at any other speed the rAF accumulator stays authoritative
  // and the soundtrack is muted rather than allowed to drift against it.
  const audioIsMaster = () => {
    const a = audioRef.current;
    return !!(a && a.duration > 0 && speedRef.current === 1);
  };

  useEffect(() => {
    if (!isPlaying || duration <= 0) return undefined;

    let last = performance.now();
    let frame;
    const tick = () => {
      const now = performance.now();
      const delta = (now - last) / 1000;
      last = now;

      if (audioIsMaster()) {
        const a = audioRef.current;
        const t = a.time();
        const end = durationRef.current;
        if (t >= end || !a.playing) {
          if (loopRef.current) {
            a.seek(0);
            a.play(0);
            setCurrentTime(0);
          } else {
            setIsPlaying(false);
            setCurrentTime(end);
            return;
          }
        } else {
          setCurrentTime(t);
        }
        frame = requestAnimationFrame(tick);
        return;
      }

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

  // Start/stop the soundtrack alongside the show.
  useEffect(() => {
    const a = audioRef.current;
    if (!a || a.duration <= 0) return;
    if (isPlaying && speedRef.current === 1) a.play(currentTimeRef.current);
    else a.pause();
    // currentTime is deliberately not a dependency: re-running this on every
    // frame would restart the source ~60x a second and stutter the audio.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPlaying, speed, audio]);

  const scrub = useCallback((t) => {
    const clamped = Math.max(0, Math.min(durationRef.current, t));
    setCurrentTime(clamped);
    // seek() resumes playback itself if the track was already playing, so a
    // scrub mid-playback keeps the audio running from the new position rather
    // than dropping it. Doing this here rather than in an effect on
    // currentTime avoids restarting the source on every rAF frame.
    const a = audioRef.current;
    if (a && a.duration > 0) a.seek(clamped);
  }, []);

  const rewind = useCallback(() => {
    setCurrentTime(0);
    const a = audioRef.current;
    if (a && a.duration > 0) a.seek(0);
  }, []);
  const togglePlay = useCallback(() => setIsPlaying((p) => !p), []);
  const toggleLoop = useCallback(() => setIsLooping((l) => !l), []);

  return {
    currentTime, isPlaying, speed, isLooping,
    setSpeed, scrub, rewind, togglePlay, toggleLoop, setIsPlaying,
  };
}
