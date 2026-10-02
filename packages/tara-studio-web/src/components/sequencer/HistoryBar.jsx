import { Undo2, Redo2, History } from 'lucide-react';

/**
 * Undo/redo buttons and the edit history list (issue #100).
 *
 * The list shows every recorded edit, oldest first. Entries past the current
 * point have been undone and are dimmed; clicking any entry jumps the document
 * to the state right after it, and "Opened" returns to the state before the
 * first edit.
 */
export default function HistoryBar({ history }) {
  const { canUndo, canRedo, undo, redo, entries, applied, jumpTo } = history;

  return (
    <div className="history-bar">
      <div className="history-actions">
        <button
          type="button" className="btn-secondary btn-sm" onClick={undo} disabled={!canUndo}
          title="Undo (Ctrl+Z)" aria-label="Undo"
        >
          <Undo2 size={13} /> Undo
        </button>
        <button
          type="button" className="btn-secondary btn-sm" onClick={redo} disabled={!canRedo}
          title="Redo (Ctrl+Shift+Z)" aria-label="Redo"
        >
          <Redo2 size={13} /> Redo
        </button>
      </div>

      <details className="history-list">
        <summary>
          <History size={12} /> History
          <span className="history-count">{applied}/{entries.length}</span>
        </summary>
        <ol>
          <li>
            <button
              type="button" onClick={() => jumpTo(0)}
              className={applied === 0 ? 'current' : ''}
              aria-current={applied === 0 ? 'step' : undefined}
            >
              Opened
            </button>
          </li>
          {entries.map((entry, i) => (
            // Entries are append-only between resets, so position is a stable key.
            // eslint-disable-next-line react/no-array-index-key
            <li key={i}>
              <button
                type="button" onClick={() => jumpTo(i + 1)}
                className={i + 1 === applied ? 'current' : i >= applied ? 'undone' : ''}
                aria-current={i + 1 === applied ? 'step' : undefined}
              >
                {entry.label}
              </button>
            </li>
          ))}
        </ol>
      </details>
    </div>
  );
}
