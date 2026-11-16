import { useState, useEffect, useCallback } from 'react';
import { API_BASE } from '../config';
import { getSession, gcsFetch } from '../session';
import { hoverErrorText } from '../hoverSequence';

/**
 * Hook managing GCS Operating Mode (SIMULATION vs HARDWARE) and telemetry freshness (issue #131).
 */
export default function useOperatingMode(setErrorMsg) {
  const [modeInfo, setModeInfo] = useState({
    mode: 'SIMULATION',
    backend: 'sim',
    fidelity: 'kinematic',
    hardwareProfile: null,
    hardwareFlightCertified: false,
    telemetryFreshnessS: null,
    telemetryStatus: 'FRESH',
    rcTakeoverAvailable: true,
    // false until the GCS has reported its mode once: the defaults above are a guess, and a control that
    // starts a flight must not act on a guess.
    loaded: false,
  });

  const fetchMode = useCallback(async () => {
    const session = getSession();
    try {
      const res = await fetch(`${API_BASE}/api/mode`, {
        headers: session ? { Authorization: `Bearer ${session.token}` } : {},
      });
      if (res.ok) {
        const data = await res.json();
        setModeInfo({
          mode: data.mode,
          backend: data.backend,
          fidelity: data.fidelity,
          hardwareProfile: data.hardware_profile,
          hardwareFlightCertified: data.hardware_flight_certified,
          telemetryFreshnessS: data.telemetry_freshness_s,
          telemetryStatus: data.telemetry_status,
          rcTakeoverAvailable: data.rc_takeover_available,
          loaded: true,
        });
      }
    } catch {
      // Background poll failure is non-fatal
    }
  }, []);

  useEffect(() => {
    fetchMode();
    const interval = setInterval(fetchMode, 2000);
    return () => clearInterval(interval);
  }, [fetchMode]);

  const switchMode = async (targetMode) => {
    const session = getSession();
    const res = await fetch(`${API_BASE}/api/mode`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(session ? { Authorization: `Bearer ${session.token}` } : {}),
      },
      body: JSON.stringify({
        mode: targetMode,
        confirmation: `CONFIRM_${targetMode.toUpperCase()}`,
      }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = body.detail || 'Failed to switch mode.';
      if (setErrorMsg) setErrorMsg(err);
      throw new Error(err);
    }
    await fetchMode();
    return body;
  };

  // The supervised single-drone hover (#171). Resolves with the GCS's answer (status COMPLETED and the steps it
  // took); rejects with the GCS's own refusal text. It does not touch the dashboard's error banner: the control
  // that calls it shows the outcome next to itself. A 401 ends the session through gcsFetch like every GCS call.
  const triggerSingleDroneDemo = async (targetAltM = 1.0, hoverDurationS = 5.0) => {
    const res = await gcsFetch(`${API_BASE}/api/single-drone/hover-sequence`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        target_altitude_m: targetAltM,
        hover_duration_s: hoverDurationS,
      }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(hoverErrorText(body.detail, `The hover sequence was refused (HTTP ${res.status}).`));
    return body;
  };

  return {
    ...modeInfo,
    refresh: fetchMode,
    switchMode,
    triggerSingleDroneDemo,
  };
}
