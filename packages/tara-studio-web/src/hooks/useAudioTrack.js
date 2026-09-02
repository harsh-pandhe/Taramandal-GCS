import { useState, useRef, useCallback, useEffect } from 'react';
import { AudioClock, computePeaks, readFileAsArrayBuffer } from '../engine/audioEngine.js';

/**
 * Loads a soundtrack and exposes it as a transport the show clock can follow.
 *
 * Owns one AudioClock for the lifetime of the component. `peaks` is computed
 * once per file — it's the expensive part — and the waveform canvas redraws
 * from it on every zoom/scrub without touching the decoded buffer again.
 */
export default function useAudioTrack() {
  const clockRef = useRef(null);
  if (clockRef.current === null) clockRef.current = new AudioClock();

  const [name, setName] = useState('');
  const [duration, setDuration] = useState(0);
  const [peaks, setPeaks] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [volume, setVolumeState] = useState(1);

  useEffect(() => {
    const clock = clockRef.current;
    // Browsers cap concurrent AudioContexts, so release ours on unmount.
    return () => clock.dispose();
  }, []);

  const load = useCallback(async (file) => {
    if (!file) return;
    setLoading(true);
    setError('');
    try {
      const raw = await readFileAsArrayBuffer(file);
      const secs = await clockRef.current.load(raw);
      // ~8000 buckets is enough detail for a 5x zoom on a wide timeline while
      // staying small enough to redraw every frame during a scrub.
      setPeaks(computePeaks(clockRef.current.buffer, 8000));
      setDuration(secs);
      setName(file.name);
    } catch (e) {
      // A corrupt or unsupported file must leave the previous track intact
      // rather than half-replacing it.
      setError(e?.message || 'Could not decode that audio file.');
    } finally {
      setLoading(false);
    }
  }, []);

  const clear = useCallback(() => {
    clockRef.current.stop();
    clockRef.current.buffer = null;
    setName('');
    setDuration(0);
    setPeaks(null);
    setError('');
  }, []);

  const setVolume = useCallback((v) => {
    clockRef.current.setVolume(v);
    setVolumeState(clockRef.current.volume);
  }, []);

  return {
    clock: clockRef.current,
    name, duration, peaks, loading, error, volume,
    hasTrack: !!peaks,
    load, clear, setVolume,
  };
}
