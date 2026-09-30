/**
 * "operations" is the telemetry/control dashboard; "earth" is the Phase 4
 * georeferenced 3D globe; "night" is the Phase 5 cinematic ground-level
 * audience view.
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
    </div>
  );
}
