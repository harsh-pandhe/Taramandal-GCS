/**
 * The checklist override.
 */
export default function ChecklistBypass({ bypassChecklist, setBypassChecklist }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginTop: '0.75rem' }}>
      <input
        type="checkbox"
        id="bypass"
        checked={bypassChecklist}
        onChange={(e) => setBypassChecklist(e.target.checked)}
        style={{ accentColor: 'var(--primary)', cursor: 'pointer' }}
      />
      <label htmlFor="bypass" style={{ fontSize: '0.85rem', color: 'var(--text-muted)', cursor: 'pointer' }}>
        Bypass launch-geometry check for START SHOW (For testing/simulation)
      </label>
    </div>
  );
}
