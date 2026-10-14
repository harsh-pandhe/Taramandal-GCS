import { useState } from 'react';
import EarthView from './views/EarthView';
import NightSkyView from './views/NightSkyView';
import HeaderBar from './components/layout/HeaderBar';
import ViewTabs from './components/layout/ViewTabs';
import ErrorBanner from './components/alerts/ErrorBanner';
import SimControlBar from './components/sim/SimControlBar';
import FlightOperationsPanel from './components/actions/FlightOperationsPanel';
import FleetTelemetryPanel from './components/fleet/FleetTelemetryPanel';
import PreflightChecklistPanel from './components/preflight/PreflightChecklistPanel';
import TrajectoryPlannerPanel from './components/mission/TrajectoryPlannerPanel';
import { PreflightModal } from './PreflightChecklist';
import { usePreflight } from './preflight';
import { API_BASE } from './config';
import useTelemetrySocket from './hooks/useTelemetrySocket';
import useSimControl from './hooks/useSimControl';
import useMission from './hooks/useMission';
import { computeFleetStats } from './fleetStats';
import LoginScreen from './components/auth/LoginScreen';
import { logout, useSession } from './session';
import useOperatingMode from './hooks/useOperatingMode';

/**
 * Root layout of the GCS operator dashboard. Owns only the state shared
 * across groups (the error banner, the view tab, the checklist override);
 * each group's own state lives in its hook or component:
 *
 *   header / alerts        HeaderBar, ErrorBanner
 *   simulator              SimControlBar            (useSimControl)
 *   primary + emergency    FlightOperationsPanel
 *   fleet state            FleetTelemetryPanel      (useTelemetrySocket)
 *   readiness              PreflightChecklistPanel
 *   mission                TrajectoryPlannerPanel   (useMission)
 */
/** Sign-in gate (issue #64): nothing below talks to the GCS until an operator is logged in. */
export default function App() {
  const session = useSession();
  return session ? <Dashboard session={session} /> : <LoginScreen />;
}

function Dashboard({ session }) {
  const [errorMsg, setErrorMsg] = useState('');
  const [bypassChecklist, setBypassChecklist] = useState(false);
  const [viewMode, setViewMode] = useState('operations');
  const [showPreflight, setShowPreflight] = useState(false);

  const { report: preflight, error: preflightError } = usePreflight(API_BASE);
  const { telemetry, fleetBufferRef, wsConnected, fleetState } = useTelemetrySocket(setErrorMsg);
  const mission = useMission(setErrorMsg, fleetState);
  const sim = useSimControl(setErrorMsg);
  const operatingMode = useOperatingMode(setErrorMsg);

  const stats = computeFleetStats(telemetry);

  return (
    <div className="app-container">
      <HeaderBar
        wsConnected={wsConnected}
        fleetState={fleetState}
        session={session}
        onLogout={logout}
        mode={operatingMode.mode}
        telemetryStatus={operatingMode.telemetryStatus}
      />

      <ErrorBanner message={errorMsg} />

      <SimControlBar {...sim} />

      <ViewTabs viewMode={viewMode} onChange={setViewMode} />

      {viewMode === 'earth' && (
        <div className="earth-view-panel">
          <EarthView telemetry={telemetry} geofenceRadiusM={mission.geofenceRadii?.hard_geofence_radius_m ?? 30.0} />
        </div>
      )}

      {viewMode === 'night' && (
        <div className="earth-view-panel">
          <NightSkyView telemetry={telemetry} fleetBuffer={fleetBufferRef} />
        </div>
      )}

      {/* Main Grid Layout */}
      <div className="dashboard-grid" style={{ display: viewMode === 'operations' ? undefined : 'none' }}>
        {/* Left side: Flight Controls and Fleet Status */}
        <main className="main-column">
          <FlightOperationsPanel
            connectedCount={stats.connectedCount}
            droneCount={stats.droneIds.length}
            onOpenPreflight={() => setShowPreflight(true)}
            triggerAction={mission.triggerAction}
            bypassChecklist={bypassChecklist}
            setBypassChecklist={setBypassChecklist}
          />

          {showPreflight && (
            <PreflightModal
              apiBase={API_BASE}
              report={preflight}
              error={preflightError}
              onClose={() => setShowPreflight(false)}
              onLaunched={() => setErrorMsg('')}
            />
          )}

          <FleetTelemetryPanel telemetry={telemetry} stats={stats} />
        </main>

        {/* Right side: Sidebar (Checklist & Trajectory Parser) */}
        <aside className="sidebar">
          <PreflightChecklistPanel report={preflight} error={preflightError} />
          <TrajectoryPlannerPanel mission={mission} bypassChecklist={bypassChecklist} />
        </aside>
      </div>
    </div>
  );
}
