import { Plus, X } from 'lucide-react';
import { NumberField } from '../common/index.jsx';
import { MAX_FLEET_SIZE } from '../../engine/api.js';

/** Per-vehicle configuration: call sign, spawn offset, hover altitude, colour. */
export default function FleetPage({ dronesList, onAdd, onRemove, onUpdate, onSetCount }) {
  return (
    <main className="studio-viewport-page">
      <div className="page-header">
        <div>
          <h2>Swarm Fleet</h2>
          <p>Vehicle call signs, spawn offsets, hover altitude and LED colour.</p>
        </div>
        <div className="page-header-actions">
          <NumberField
            label="Fleet size" min={1} max={MAX_FLEET_SIZE}
            value={dronesList.length} onChange={(v) => onSetCount(Math.round(v))}
          />
          <button type="button" className="btn-primary btn-inline" onClick={onAdd} disabled={dronesList.length >= MAX_FLEET_SIZE}>
            <Plus size={15} /> Add Vehicle
          </button>
        </div>
      </div>

      <div className="fleet-grid">
        {dronesList.map((drone, idx) => (
          <div className="fleet-card" key={drone.id} style={{ borderLeft: `4px solid ${drone.colorHex}` }}>
            <div className="fleet-card-header">
              <span className="drone-id-badge" style={{ color: drone.colorHex }}>
                VEHICLE {String(idx).padStart(2, '0')}
              </span>
              <button
                type="button" className="delete-drone-btn"
                onClick={() => onRemove(drone.id)} disabled={dronesList.length <= 1}
                aria-label={`Remove ${drone.name}`}
              >
                <X size={15} />
              </button>
            </div>

            <label className="ui-field">
              <span className="ui-field-label">Call Sign</span>
              <input value={drone.name} onChange={(e) => onUpdate(drone.id, 'name', e.target.value)} />
            </label>

            <div className="form-row">
              <NumberField
                label="Spawn Y" suffix="m" step={0.5} value={drone.spawnOffset}
                onChange={(v) => onUpdate(drone.id, 'spawnOffset', v)}
              />
              <NumberField
                label="Hover Alt" suffix="m" step={0.1} min={0} value={drone.hoverAlt}
                onChange={(v) => onUpdate(drone.id, 'hoverAlt', v)}
              />
            </div>

            <label className="ui-field">
              <span className="ui-field-label">LED Colour</span>
              <div className="color-row">
                <input type="color" value={drone.colorHex} onChange={(e) => onUpdate(drone.id, 'colorHex', e.target.value)} />
                <span className="mono">{drone.colorHex.toUpperCase()}</span>
              </div>
            </label>
          </div>
        ))}
      </div>
    </main>
  );
}
