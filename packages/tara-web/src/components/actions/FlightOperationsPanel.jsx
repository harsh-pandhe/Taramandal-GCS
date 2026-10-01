import EmergencyActions from './EmergencyActions';
import ChecklistBypass from './ChecklistBypass';

/** Primary fleet actions: pre-arm check & takeoff, emergency commands, and the checklist override. */
export default function FlightOperationsPanel({
  connectedCount, droneCount, onOpenPreflight, triggerAction,
  bypassChecklist, setBypassChecklist,
}) {
  return (
    <section className="gcs-panel">
      <h2 className="panel-header">
        FLIGHT OPERATIONS COMMAND
        <span style={{ fontSize: '0.85rem', fontWeight: 400, color: 'var(--text-muted)' }}>
          Active Drones: {connectedCount}/{droneCount || 3}
        </span>
      </h2>

      <div className="master-controls">
        <button
          className="btn btn-launch"
          onClick={onOpenPreflight}
          data-testid="open-preflight"
        >
          PRE-ARM CHECK &amp; TAKEOFF
        </button>

        <EmergencyActions triggerAction={triggerAction} />
      </div>

      <ChecklistBypass bypassChecklist={bypassChecklist} setBypassChecklist={setBypassChecklist} />
    </section>
  );
}
