import React, { useCallback, useEffect, useRef, useState } from 'react';
import EarthView from './EarthView';
import NightSkyView from './NightSkyView';
import { MAX_NIGHTSKY_DRONES } from '../config';
import { FLOATS_PER_DRONE } from '../telemetry';
import {
  detailSpanMs,
  detailWindowFor,
  eventFields,
  eventLabel,
  fillFleetBuffer,
  formatClock,
  isBreach,
  listSessions,
  loadSession,
  loadWindow,
  pickFrames,
  telemetryAt,
  tickIntervalMs,
  timelineFraction,
  windowCovers,
} from '../replay';

const SPEEDS = [0.5, 1, 2, 5, 10];
// The scrubber's number and the clock re-render this often; the 3D view is fed
// every animation frame straight from the buffer, without going through React.
const UI_TICK_MS = 100;
// After a seek, wait for the scrubber to settle before asking the GCS for detail.
const SEEK_DEBOUNCE_MS = 200;
// Do not ask again for a failed window for this long.
const WINDOW_RETRY_MS = 5000;

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
  // Full-rate detail (issue #65): one window of every recorded tick around the
  // current time, used instead of the thinned overview while it covers the time.
  const winRef = useRef(null); // { from, to, frames }
  const sessionRef = useRef(null); // { token, id, spanMs, startMs, endMs }
  const winBusy = useRef(false);
  const winFailedAt = useRef(-Infinity);
  const seekTimer = useRef(null);
  const [windowInfo, setWindowInfo] = useState(null); // { from, to } of the loaded window
  // Which 3D view plays the recording. The night-sky view is fed every animation frame from the
  // buffer; the Earth view (a Cesium viewer) is fed a telemetry object at the 10 Hz readout rate.
  const [view, setView] = useState('night');
  const viewRef = useRef('night');
  const [earthTel, setEarthTel] = useState({});

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

  const drawAt = useCallback((ms) => {
    fillFleetBuffer(pickFrames(framesRef.current, winRef.current, ms), ms, fleetBuffer.current);
  }, []);

  // The 10 Hz readout: the clock, and the Earth view's telemetry when that view is showing.
  const publish = useCallback((ms) => {
    setTMs(ms);
    if (viewRef.current === 'earth') {
      setEarthTel(telemetryAt(pickFrames(framesRef.current, winRef.current, ms), ms));
    }
  }, []);

  // Loads the full-rate window for `ms` unless the current one already covers it
  // (with `marginMs` to spare when playing forward). One request at a time; a
  // failed or empty window is not retried for WINDOW_RETRY_MS, and the overview
  // keeps drawing meanwhile.
  const ensureWindow = useCallback(
    (ms, marginMs = 0) => {
      const s = sessionRef.current;
      if (!s || windowCovers(winRef.current, ms, marginMs) || winBusy.current) return;
      if (performance.now() - winFailedAt.current < WINDOW_RETRY_MS) return;
      const w = detailWindowFor(ms, s.spanMs, s.startMs, s.endMs);
      winBusy.current = true;
      loadWindow(s.id, w.from, w.to)
        .then((reply) => {
          if (sessionRef.current?.token !== s.token) return;
          if (!reply.frames || reply.frames.length < 2) {
            winFailedAt.current = performance.now();
            return;
          }
          winRef.current = { from: w.from, to: w.to, frames: reply.frames };
          setWindowInfo({ from: w.from, to: w.to });
          drawAt(tRef.current);
          publish(tRef.current);
        })
        .catch(() => {
          winFailedAt.current = performance.now();
        })
        .finally(() => {
          winBusy.current = false;
          // The time may have moved on while this was in flight.
          if (sessionRef.current?.token === s.token) ensureWindow(tRef.current, marginMs);
        });
    },
    [drawAt, publish],
  );

  const seek = useCallback(
    (ms) => {
      tRef.current = ms;
      drawAt(ms);
      publish(ms);
      clearTimeout(seekTimer.current);
      seekTimer.current = setTimeout(() => ensureWindow(ms), SEEK_DEBOUNCE_MS);
    },
    [drawAt, ensureWindow, publish],
  );

  useEffect(() => () => clearTimeout(seekTimer.current), []);

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
        const start = data.frames.t_ms_first ?? data.summary.first_tick_ms ?? 0;
        winRef.current = null;
        winFailedAt.current = -Infinity;
        setWindowInfo(null);
        setEarthTel({});
        sessionRef.current = {
          token: mine,
          id,
          spanMs: detailSpanMs(data.summary.drone_ids.length, tickIntervalMs(data.summary)),
          startMs: start,
          endMs: Math.max(data.summary.duration_ms, start),
        };
        setLoaded(data);
        seek(start);
      } catch (err) {
        if (mine !== loadSeq.current) return;
        framesRef.current = [];
        winRef.current = null;
        sessionRef.current = null;
        setWindowInfo(null);
        setEarthTel({});
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
      drawAt(next);
      if (now - lastUi >= UI_TICK_MS || next >= endMs) {
        lastUi = now;
        publish(next);
        // Fetch the next window before this one runs out.
        if (sessionRef.current) ensureWindow(next, sessionRef.current.spanMs * 0.15);
      }
      if (next >= endMs) {
        setPlaying(false);
        return;
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [playing, loaded, speed, endMs, drawAt, ensureWindow, publish]);

  const atEnd = loaded && tMs >= endMs;
  const togglePlay = () => {
    if (!loaded) return;
    if (atEnd) seek(startMs);
    setPlaying((p) => !p);
  };

  const changeView = (next) => {
    viewRef.current = next;
    setView(next);
    if (next === 'earth') setEarthTel(telemetryAt(pickFrames(framesRef.current, winRef.current, tRef.current), tRef.current));
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
        {view === 'earth' ? (
          <EarthView key={loaded?.summary.id ?? 'none'} telemetry={earthTel} />
        ) : (
          <NightSkyView telemetry={{}} fleetBuffer={fleetBuffer} showDeviationHud={false} />
        )}
        {!loaded && (
          <div className="replay-empty">
            {loading ? 'Loading recording…' : 'Pick a recorded flight to replay it here.'}
          </div>
        )}
      </div>

      <div className="replay-controls" data-testid="replay-controls">
        <div className="replay-row" role="group" aria-label="3D view">
          <span className="replay-label">VIEW</span>
          {[['night', 'NIGHT SKY'], ['earth', 'EARTH']].map(([id, label]) => (
            <button
              key={id}
              className={`view-tab ${view === id ? 'active' : ''}`}
              aria-pressed={view === id}
              onClick={() => changeView(id)}
            >
              {label}
            </button>
          ))}
        </div>
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
              <div className="replay-note" data-testid="replay-resolution">
                {windowInfo && tMs >= windowInfo.from && tMs <= windowInfo.to
                  ? `Showing every recorded sample from ${formatClock(windowInfo.from - startMs)} to ${formatClock(windowInfo.to - startMs)}.`
                  : `Overview: one sample per ${((loaded.frames.stride * tickIntervalMs(loaded.summary)) / 1000).toFixed(1)} s, positions blended between them. Full detail loads around the current time.`}
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
