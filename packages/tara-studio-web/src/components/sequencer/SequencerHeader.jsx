import { Clock, Users, Gauge, Film } from 'lucide-react';
import { Badge } from '../common/index.jsx';
import { exportGate } from '../../engine/exportGate.js';

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
 *
 * `predictionRefused` (issue #141 review): the scene list, as it stands
 * right now, cannot be built at all (a ShowRefusalError) - predictedDuration
 * is then just 0, which on its own would read as "a real, instant show",
 * not "no prediction". Shown as "—" plus a Refused badge instead.
 */
export default function SequencerHeader({
  sceneCount, numDrones, rate, predictedDuration, builtDuration, stale,
  predictionRefused, refusalMessage, hasShow = false, validation = null, validating = false,
}) {
  const gate = exportGate({ hasShow, validation });
  return (
    <div className="sequencer-header">
      <div className="seq-stat">
        <Clock size={13} />
        <div>
          <span className="seq-stat-label">Show length</span>
          <span className="seq-stat-value">{predictionRefused ? '—' : fmtDuration(predictedDuration)}</span>
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

      {predictionRefused ? (
        <Badge tone="danger" title={refusalMessage || 'The current scene list cannot be built'}>
          Refused
        </Badge>
      ) : stale ? (
        <Badge tone="warn" title="The scene list changed since the last build">
          Rebuild needed
        </Badge>
      ) : builtDuration > 0 ? (
        <Badge tone="ok" title="Built trajectory matches the scene list">
          Built · {fmtDuration(builtDuration)}
        </Badge>
      ) : null}

      {/* Same predicate as the Transmit tab's buttons (engine/exportGate.js), and
          worded to say what the buttons do, no more: "enabled", not "safe to fly" or
          "will succeed" - the server still re-checks the show on export. */}
      {validating ? (
        <Badge tone="default" title="Safety validation is running">
          Validating…
        </Badge>
      ) : gate.kind === 'open' ? (
        <Badge tone="ok" title="The Transmit tab's export buttons are enabled. The server re-checks the show when you export and can still refuse it.">
          Safety passed · exports enabled
        </Badge>
      ) : gate.kind === 'failed' ? (
        <Badge
          tone="danger"
          title={gate.notFlightCertified
            ? 'The show clears its hard limits but is not flight-certified (jerk or feasibility). Signed exports are locked; see the Transmit tab.'
            : 'Fix the violations in the Safety tab before exporting.'}
        >
          {gate.notFlightCertified ? 'Not flight-certified' : 'Safety failed'} · exports locked
        </Badge>
      ) : gate.kind === 'unverified' ? (
        <Badge tone="warn" title="There is no passing safety validation for this show yet; the export buttons are locked.">
          Unverified · exports locked
        </Badge>
      ) : null}
    </div>
  );
}
