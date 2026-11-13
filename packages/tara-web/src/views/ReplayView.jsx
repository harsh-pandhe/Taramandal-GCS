import React, { useCallback, useEffect, useRef, useState } from 'react';
import NightSkyView from './NightSkyView';
import { MAX_NIGHTSKY_DRONES } from '../config';
import { FLOATS_PER_DRONE } from '../telemetry';
import {
  eventFields,
  eventLabel,
  fillFleetBuffer,
  formatClock,
  isBreach,
  listSessions,
  loadSession,
  timelineFraction,
} from '../replay';

const SPEEDS = [0.5, 1, 2, 5, 10];
// The scrubber's number and the clock re-render this often; the 3D view is fed
// every animation frame straight from the buffer, without going through React.
const UI_TICK_MS = 100;

function sessionLabel(s) {
  const when = s.started ? new Date(s.started).toLocaleString() : s.id;
  return `${when}${s.live ? ' (recording now)' : ''}`;
}

/**
 * Replay mode (issue #65): pick a recorded flight, scrub or play it in the
 * night-sky view, and jump to the safety events pinned on the timeline.
 * Everything shown comes from the GCS's read-only /api/blackbox/* routes;
 * operator identities in the recording are hidden by the GCS.
 */
export default function ReplayView() {
  const [sessions, setSessions] = useState(null);
  const [listError, setListError] = useState('');
  const [selectedId, setSelectedId] = useState('');
  const [loaded, setLoaded] = useState(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [tMs, setTMs] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [selectedEvent, setSelectedEvent] = useState(null);

  const fleetBuffer = useRef(null);
  if (fleetBuffer.current === null) {
    fleetBuffer.current = { data: new Float32Array(MAX_NIGHTSKY_DRONES * FLOATS_PER_DRONE), count: 0 };
  }
  const tRef = useRef(0);
  // Only the latest LOAD may apply its result; an older, slower response is dropped.
  const loadSeq = useRef(0);
  const framesRef = useRef([]);

  useEffect(() => {
    let cancelled = false;
    listSessions()
      .then((rows) => {
        if (cancelled) return;
        setSessions(rows);
        if (rows.length) setSelectedId(rows[0].id);
      })
      .catch((err) => !cancelled && setListError(err.message));
    return () => {
      cancelled = true;
    };
  }, []);

  const seek = useCallback((ms) => {
    tRef.current = ms;
    fillFleetBuffer(framesRef.current, ms, fleetBuffer.current);
    setTMs(ms);
  }, []);

  const load = useCallback(
    async (id) => {
      const mine = ++loadSeq.current;
      setLoading(true);
      setLoadError('');
      setPlaying(false);
      setSelectedEvent(null);
      try {
        const data = await loadSession(id);
        if (mine !== loadSeq.current) return;
        framesRef.current = data.frames.frames;
        setLoaded(data);
        seek(data.frames.t_ms_first ?? data.summary.first_tick_ms ?? 0);
      } catch (err) {
        if (mine !== loadSeq.current) return;
        framesRef.current = [];
        fleetBuffer.current.count = 0;
        setLoaded(null);
        setLoadError(err.message);
      } finally {
        if (mine === loadSeq.current) setLoading(false);
      }
    },
    [seek],
  );

  const startMs = loaded ? (loaded.frames.t_ms_first ?? loaded.summary.first_tick_ms ?? 0) : 0;
  const endMs = loaded ? Math.max(loaded.summary.duration_ms, startMs) : 0;

  // Playback: advance the clock by wall time x speed, feed the 3D view every
  // animation frame, and update the readout at UI_TICK_MS.
  useEffect(() => {
    if (!playing || !loaded) return undefined;
    let raf;
    let last = performance.now();
    let lastUi = last;
    const step = (now) => {
      const next = Math.min(endMs, tRef.current + (now - last) * speed);
      last = now;
      tRef.current = next;
      fillFleetBuffer(framesRef.current, next, fleetBuffer.current);
      if (now - lastUi >= UI_TICK_MS || next >= endMs) {
        lastUi = now;
        setTMs(next);
      }
      if (next >= endMs) {
        setPlaying(false);
        return;
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [playing, loaded, speed, endMs]);

  const atEnd = loaded && tMs >= endMs;
  const togglePlay = () => {
    if (!loaded) return;
    if (atEnd) seek(startMs);
    setPlaying((p) => !p);
  };

  const events = loaded?.events.events ?? [];
  const jumpTo = (event) => {
    setPlaying(false);
    setSelectedEvent(event);
    seek(event.t);
  };

  return (
    <div className="replay-view">
      <div className="replay-stage earth-view-panel" data-testid="replay-stage">
        <NightSkyView telemetry={{}} fleetBuffer={fleetBuffer} showDeviationHud={false} />
        {!loaded && (
          <div className="replay-empty">
            {loading ? 'Loading recording…' : 'Pick a recorded flight to replay it here.'}
          </div>
        )}
      </div>

      <div className="replay-controls" data-testid="replay-controls">
        <div className="replay-row">
          <label className="replay-label" htmlFor="replay-session">
            RECORDING
          </label>
          <select
            id="replay-session"
            className="replay-select"
            value={selectedId}
            onChange={(e) => setSelectedId(e.target.value)}
            disabled={!sessions?.length}
          >
            {(sessions ?? []).map((s) => (
              <option key={s.id} value={s.id}>
                {sessionLabel(s)}
              </option>
            ))}
          </select>
          <button className="view-tab" onClick={() => load(selectedId)} disabled={!selectedId || loading}>
            {loading ? 'LOADING…' : 'LOAD'}
          </button>
        </div>

        {listError && <div className="replay-note replay-note-bad">Could not list recordings: {listError}</div>}
        {sessions && !sessions.length && !listError && (
          <div className="replay-note">No recorded flights yet. A session is written while the GCS runs.</div>
        )}
        {loadError && <div className="replay-note replay-note-bad">Could not load this recording: {loadError}</div>}

        {loaded && (
          <>
            {loaded.summary.truncated && (
              <div className="replay-note">
                This recording was cut short (still recording, or the GCS stopped abnormally); it is shown as far as it
                could be read.
              </div>
            )}
            {loaded.frames.stride > 1 && (
              <div className="replay-note">
                Long recording: showing every {loaded.frames.stride}th sample, positions blended between them.
              </div>
            )}
            <div className="replay-row">
              <button className="view-tab active" onClick={togglePlay} aria-label={playing ? 'Pause' : 'Play'}>
                {playing ? 'PAUSE' : atEnd ? 'REPLAY' : 'PLAY'}
              </button>
              <select
                className="replay-select replay-speed"
                value={speed}
                onChange={(e) => setSpeed(Number(e.target.value))}
                aria-label="Playback speed"
              >
                {SPEEDS.map((s) => (
                  <option key={s} value={s}>
                    {s}x
                  </option>
                ))}
              </select>
              <span className="replay-clock" data-testid="replay-clock">
                {formatClock(tMs - startMs)} / {formatClock(endMs - startMs)}
              </span>
              <span className="replay-meta">
                {loaded.summary.drone_ids.length} drones · {loaded.summary.events} events recorded
              </span>
            </div>

            <div className="replay-timeline">
              <input
                type="range"
                className="replay-scrubber"
                aria-label="Timeline"
                min={startMs}
                max={endMs}
                step={1}
                value={tMs}
                onChange={(e) => {
                  setPlaying(false);
                  seek(Number(e.target.value));
                }}
              />
              <div className="replay-markers">
                {events.map((event, i) => (
                  <button
                    key={`${event.t}-${event.kind}-${i}`}
                    className={`replay-marker ${isBreach(event) ? 'breach' : 'clear'}${event === selectedEvent ? ' selected' : ''}`}
                    style={{ left: `${timelineFraction(event.t, startMs, endMs) * 100}%` }}
                    title={`${formatClock(event.t - startMs)} ${eventLabel(event)}`}
                    aria-label={`${eventLabel(event)} at ${formatClock(event.t - startMs)}`}
                    onClick={() => jumpTo(event)}
                  />
                ))}
              </div>
            </div>
            <div className="replay-note">
              {events.length
                ? `${events.length} safety event${events.length === 1 ? '' : 's'} on the timeline${
                    loaded.events.limited ? ` (first ${events.length} of ${loaded.events.total})` : ''
                  }. Click a marker to jump to it.`
                : 'No proximity or geofence events were recorded in this flight.'}
            </div>

            {selectedEvent && (
              <div className="replay-detail" data-testid="replay-detail">
                <strong className={isBreach(selectedEvent) ? 'replay-bad' : 'replay-good'}>
                  {eventLabel(selectedEvent).toUpperCase()}
                </strong>{' '}
                at {formatClock(selectedEvent.t - startMs)}
                <dl>
                  {eventFields(selectedEvent).map(([key, value]) => (
                    <React.Fragment key={key}>
                      <dt>{key}</dt>
                      <dd>{value}</dd>
                    </React.Fragment>
                  ))}
                </dl>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
