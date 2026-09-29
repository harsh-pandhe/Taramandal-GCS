import { useEffect, useState } from 'react';
import { BatteryCharging, AlertTriangle, CheckCircle } from 'lucide-react';
import { NumberField } from '../common/index.jsx';
import { validateEnergy } from '../../engine/api.js';
import { loadBatterySettings, saveBatterySettings } from '../../engine/energy.js';
import BatteryTimeline from './BatteryTimeline.jsx';

// Settings edits re-run the model; wait for the designer to stop typing.
const DEBOUNCE_MS = 400;

const Metric = ({ title, value, unit, ok }) => (
  <div className="metric-card">
    <div className="metric-title">{title}</div>
    <div className={`metric-val ${ok ? 'passed' : 'warning'}`}>{value}{unit}</div>
  </div>
);

/**
 * Battery endurance for the built show (issue #54): energy, peak current and
 * the remaining-charge timeline, judged against the pack the designer says
 * the fleet will fly on.
 */
export default function EnduranceSection({ choreoData }) {
  const [battery, setBattery] = useState(loadBatterySettings);
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const update = (key) => (value) => setBattery((prev) => ({ ...prev, [key]: value }));

  useEffect(() => { saveBatterySettings(battery); }, [battery]);

  useEffect(() => {
    if (!choreoData) return undefined;
    let cancelled = false;
    const timer = setTimeout(() => {
      setLoading(true);
      validateEnergy(choreoData, battery)
        .then((r) => { if (!cancelled) { setReport(r); setError(null); } })
        .catch((e) => { if (!cancelled) { setReport(null); setError(e.message); } })
        .finally(() => { if (!cancelled) setLoading(false); });
    }, DEBOUNCE_MS);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [choreoData, battery]);

  const m = report?.metrics;
  const v = report?.violations;
  const passed = report?.status === 'PASSED';
  const limits = report?.limits;

  return (
    <div className="endurance-section">
      <h4 className="section-subtitle">Battery Endurance</h4>

      <div className="form-row">
        <label className="ui-field">
          <span className="ui-field-label">Chemistry</span>
          <select value={battery.chemistry} onChange={(e) => update('chemistry')(e.target.value)}>
            <option value="lipo">LiPo</option>
            <option value="liion">Li-ion</option>
          </select>
        </label>
        <NumberField label="Cells" suffix="S" value={battery.cells_series} onChange={update('cells_series')} min={1} max={14} />
        <NumberField label="Capacity" suffix="mAh" value={battery.capacity_mah} onChange={update('capacity_mah')} min={100} max={100000} step={100} />
        <NumberField label="Ambient" suffix="°C" value={battery.ambient_temp_c} onChange={update('ambient_temp_c')} min={-40} max={60} />
      </div>

      {error && <div className="validation-summary failed"><AlertTriangle size={18} /><span>{error}</span></div>}
      {!report && !error && (
        <p className="panel-hint">{loading ? 'Estimating power draw…' : 'Waiting for the endurance estimate…'}</p>
      )}

      {report && m && (
        <>
          <div className={`validation-summary ${report.status.toLowerCase()}`} aria-live="polite">
            {passed ? <CheckCircle size={18} /> : <AlertTriangle size={18} />}
            <span>{report.summary}</span>
          </div>

          <div className="metric-grid">
            <Metric title="Energy (fleet)" value={m.total_energy_wh} unit=" Wh" ok />
            <Metric title="Peak Current" value={m.peak_current_a} unit=" A" ok={m.peak_c_rate <= limits.max_c_rate} />
            <Metric title="Min Remaining" value={m.min_remaining_pct} unit="%" ok={m.max_depth_of_discharge_pct <= limits.max_depth_of_discharge_pct} />
            <Metric title="Min Cell V" value={m.min_cell_voltage_v.toFixed(2)} unit=" V" ok={m.min_cell_voltage_v >= limits.min_cell_voltage_v} />
          </div>

          {report.timeline?.time_s?.length > 1 && (
            <BatteryTimeline
              timeline={report.timeline}
              reservePct={Math.round(100 - limits.max_depth_of_discharge_pct)}
            />
          )}

          {!passed && (
            <div className="violations-list">
              {v.depth_of_discharge.map((x) => (
                <div key={`d-${x.drone}`} className="violation-item">
                  <span>Drain · Drone {x.drone} ({x.depth_of_discharge_pct}%)</span>
                  <span className="v-danger">cut ≥{x.required_reduction_s}s</span>
                </div>
              ))}
              {v.c_rate.map((x) => (
                <div key={`c-${x.drone}`} className="violation-item">
                  <span>C-rate · Drone {x.drone}</span>
                  <span className="v-warn">{x.c_rate}C @ {x.time}s</span>
                </div>
              ))}
              {v.voltage.map((x) => (
                <div key={`v-${x.drone}`} className="violation-item">
                  <span>Voltage sag · Drone {x.drone}</span>
                  <span className="v-warn">{x.cell_voltage_v}V @ {x.time}s</span>
                </div>
              ))}
            </div>
          )}

          <p className="panel-hint">
            <BatteryCharging size={11} /> Hover draws {m.hover_power_w} W per drone; {m.usable_capacity_mah} mAh usable
            at {battery.ambient_temp_c}°C. Estimates assume still air and an X500-class airframe.
          </p>
        </>
      )}
    </div>
  );
}
