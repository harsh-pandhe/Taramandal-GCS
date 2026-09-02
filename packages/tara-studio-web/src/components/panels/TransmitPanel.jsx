import { Download, Globe, Activity } from 'lucide-react';
import { NumberField } from '../common/index.jsx';

/** Venue georeference plus export / GCS upload actions. */
export default function TransmitPanel({
  homeLat, homeLon, onHomeLat, onHomeLon,
  onExport, onSendToGCS, sending, hasShow,
}) {
  return (
    <div className="glass-panel-inner">
      <h4 className="section-subtitle">Venue Calibration</h4>
      <div className="form-row">
        <NumberField label="Venue Lat" value={homeLat} onChange={onHomeLat} step={0.0001} min={-90} max={90} />
        <NumberField label="Venue Lon" value={homeLon} onChange={onHomeLon} step={0.0001} min={-180} max={180} />
      </div>

      <h4 className="section-subtitle">Export &amp; Transmit</h4>
      <div className="form-row">
        <button type="button" className="btn-secondary btn-inline" onClick={() => onExport('json')} disabled={!hasShow}>
          <Download size={14} /> JSON
        </button>
        <button type="button" className="btn-secondary btn-inline" onClick={() => onExport('csv')} disabled={!hasShow}>
          <Download size={14} /> CSV
        </button>
      </div>

      <button type="button" className="btn-primary btn-inline" onClick={() => onExport('kml')} disabled={!hasShow}>
        <Globe size={15} /> Export Google Earth KML
      </button>

      <button type="button" className="btn-primary btn-inline btn-gcs" onClick={onSendToGCS} disabled={sending || !hasShow}>
        <Activity size={15} /> {sending ? 'Sending…' : 'Transmit to GCS Fleet'}
      </button>

      <p className="panel-hint">
        Exports carry baked LED colour per waypoint, so the light choreography
        travels with the trajectory.
      </p>
    </div>
  );
}
