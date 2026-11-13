/**
 * "operations" is the telemetry/control dashboard; "earth" is the Phase 4
 * georeferenced 3D globe; "night" is the Phase 5 cinematic ground-level
 * audience view; "replay" plays back a recorded flight (issue #65).
 */
export default function ViewTabs({ viewMode, onChange }) {
  return (
    <div className="view-tabs">
      <button className={`view-tab ${viewMode === 'operations' ? 'active' : ''}`} onClick={() => onChange('operations')}>
        OPERATIONS
      </button>
      <button className={`view-tab ${viewMode === 'earth' ? 'active' : ''}`} onClick={() => onChange('earth')}>
        3D EARTH VIEW
      </button>
      <button className={`view-tab ${viewMode === 'night' ? 'active' : ''}`} onClick={() => onChange('night')}>
        NIGHT SKY
      </button>
      <button className={`view-tab ${viewMode === 'replay' ? 'active' : ''}`} onClick={() => onChange('replay')}>
        REPLAY
      </button>
    </div>
  );
}
