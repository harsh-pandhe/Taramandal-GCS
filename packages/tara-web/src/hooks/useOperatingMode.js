import { useState, useEffect, useCallback } from 'react';
import { API_BASE } from '../config';
import { getSession } from '../session';

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

  const triggerSingleDroneDemo = async (targetAltM = 1.0, hoverDurationS = 5.0) => {
    const session = getSession();
    const res = await fetch(`${API_BASE}/api/single-drone/hover-sequence`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(session ? { Authorization: `Bearer ${session.token}` } : {}),
      },
      body: JSON.stringify({
        target_altitude_m: targetAltM,
        hover_duration_s: hoverDurationS,
      }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = body.detail || 'Single-drone sequence rejected.';
      if (setErrorMsg) setErrorMsg(err);
      throw new Error(err);
    }
    return body;
  };

  return {
    ...modeInfo,
    refresh: fetchMode,
    switchMode,
    triggerSingleDroneDemo,
  };
}
