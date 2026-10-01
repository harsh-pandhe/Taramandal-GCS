import { InterlockCards, ProfileBadge } from '../../PreflightChecklist';

/** Pre-arm interlocks HUD, live from GET /api/preflight-check */
export default function PreflightChecklistPanel({ report, error }) {
  return (
    <section className="gcs-panel">
      <h2 className="panel-header">
        PRE-ARM INTERLOCKS
        <ProfileBadge report={report} />
      </h2>
      <InterlockCards report={report} error={error} />
    </section>
  );
}
