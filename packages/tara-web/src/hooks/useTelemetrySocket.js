import { useEffect, useRef, useState } from 'react';
import { decodeFastFrame, decodeFastFrameInto, FLOATS_PER_DRONE } from '../telemetry';
import { MAX_NIGHTSKY_DRONES, WS_URL } from '../config';

/**
 * The GCS telemetry WebSocket: connection state, the per-drone telemetry map
 * the panels read, the flat fast-frame buffer the night-sky view reads, and
 * the slow-frame lifecycle state.
 * Reconnects every 2s while the GCS is unreachable.
 *
 * @param {(msg: string) => void} setErrorMsg cleared when the link comes up.
 */
export default function useTelemetrySocket(setErrorMsg) {
  const [telemetry, setTelemetry] = useState({});
  // A zero-copy mirror of the latest fast frame, for the night-sky viewport.
  // The object-shaped `telemetry` state above stays the source of truth for the
  // dashboard's panels, but at 1000 drones it is far too expensive to rebuild
  // per frame — the 3D view reads this flat Float32Array directly instead.
  // MAX_NIGHTSKY_DRONES matches NightSkyView's instance capacity.
  const fleetBufferRef = useRef(null);
  if (fleetBufferRef.current === null) {
    fleetBufferRef.current = {
      data: new Float32Array(MAX_NIGHTSKY_DRONES * FLOATS_PER_DRONE),
      count: 0,
    };
  }
  const [wsConnected, setWsConnected] = useState(false);
  // Lifecycle state from the slow channel: { state, description, allowed_commands, ... }.
  // null until the first slow frame arrives (or while the socket is down).
  const [fleetState, setFleetState] = useState(null);

  // WebSocket Connection
  useEffect(() => {
    let ws;
    let reconnectTimer;

    function connect() {
      ws = new WebSocket(WS_URL);
      // Binary frames arrive as ArrayBuffer instead of the default Blob, so
      // decodeFastFrame can read them directly without an async Blob->buffer
      // conversion first.
      ws.binaryType = 'arraybuffer';

      ws.onopen = () => {
        setWsConnected(true);
        setErrorMsg('');
        console.log('Telemetry WebSocket connected.');
      };

      // Two channels share this socket (see tara_gcs.telemetry_codec): fast
      // binary frames (position/heading/armed/connected, ~20Hz) and slow JSON
      // frames (battery/GPS/flight-mode/latency/home, ~1Hz). Each merges into
      // its own drones' entries rather than replacing the whole telemetry
      // object, since either channel updating alone would otherwise wipe out
      // whatever the other channel last reported.
      ws.onmessage = (event) => {
        if (event.data instanceof ArrayBuffer) {
          // Feed the flat buffer first: the 3D view reads it every frame and
          // must not wait on React's state commit.
          fleetBufferRef.current.count = decodeFastFrameInto(
            event.data,
            fleetBufferRef.current.data,
          );
          const fastDrones = decodeFastFrame(event.data);
          setTelemetry((prev) => {
            const next = { ...prev };
            fastDrones.forEach((fields, id) => {
              const key = String(id);
              next[key] = { ...next[key], id, ...fields };
            });
            return next;
          });
          return;
        }
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'slow' && data.drones) {
            if (data.fleet_state) setFleetState(data.fleet_state);
            setTelemetry((prev) => {
              const next = { ...prev };
              Object.entries(data.drones).forEach(([key, fields]) => {
                next[key] = { ...next[key], id: Number(key), ...fields };
              });
              return next;
            });
          }
        } catch (err) {
          console.error('Error parsing telemetry JSON:', err);
        }
      };

      ws.onclose = () => {
        setWsConnected(false);
        setFleetState(null);
        console.log('Telemetry WebSocket closed. Reconnecting in 2s...');
        reconnectTimer = setTimeout(connect, 2000);
      };

      ws.onerror = (err) => {
        console.error('WebSocket error:', err);
        ws.close();
      };
    }

    connect();

    return () => {
      if (ws) ws.close();
      clearTimeout(reconnectTimer);
    };
    // setErrorMsg is a state setter: stable for the component's lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { telemetry, fleetBufferRef, wsConnected, fleetState };
}
