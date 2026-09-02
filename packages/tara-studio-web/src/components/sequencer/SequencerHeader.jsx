import { Clock, Users, Gauge, Film } from 'lucide-react';
import { Badge } from '../common/index.jsx';

const fmtDuration = (s) => {
  const m = Math.floor(s / 60);
  const rem = s - m * 60;
  return m > 0 ? `${m}m ${rem.toFixed(1)}s` : `${rem.toFixed(1)}s`;
};

/**
 * Global show readout: total duration, scene count, fleet size, sample rate.
 *
 * `predictedDuration` comes from the scene list; `builtDuration` from the last
 * generated show. They differ when scenes have been edited since the last
 * build, and the header says so — otherwise the timeline below would silently
 * disagree with the numbers here.
 */
export default function SequencerHeader({
  sceneCount, numDrones, rate, predictedDuration, builtDuration, stale,
}) {
  return (
    <div className="sequencer-header">
      <div className="seq-stat">
        <Clock size={13} />
        <div>
          <span className="seq-stat-label">Show length</span>
          <span className="seq-stat-value">{fmtDuration(predictedDuration)}</span>
        </div>
      </div>

      <div className="seq-stat">
        <Film size={13} />
        <div>
          <span className="seq-stat-label">Scenes</span>
          <span className="seq-stat-value">{sceneCount}</span>
        </div>
      </div>

      <div className="seq-stat">
        <Users size={13} />
        <div>
          <span className="seq-stat-label">Vehicles</span>
          <span className="seq-stat-value">{numDrones}</span>
        </div>
      </div>

      <div className="seq-stat">
        <Gauge size={13} />
        <div>
          <span className="seq-stat-label">Rate</span>
          <span className="seq-stat-value">{rate} Hz</span>
        </div>
      </div>

      {stale ? (
        <Badge tone="warn" title="The scene list changed since the last build">
          Rebuild needed
        </Badge>
      ) : builtDuration > 0 ? (
        <Badge tone="ok" title="Built trajectory matches the scene list">
          Built · {fmtDuration(builtDuration)}
        </Badge>
      ) : null}
    </div>
  );
}
