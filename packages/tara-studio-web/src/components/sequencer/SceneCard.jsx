import { GripVertical, Copy, Trash, ChevronUp, ChevronDown, Shapes } from 'lucide-react';
import { NumberField } from '../common/index.jsx';

/**
 * One keyframe scene: which formation the fleet flies, how long it holds, and
 * how long it takes to morph in.
 *
 * Reordering is available two ways on purpose. Drag and drop is the fast path,
 * but it's mouse-only and invisible to keyboard and screen-reader users, so the
 * up/down buttons stay as the accessible equivalent rather than being replaced.
 */
export default function SceneCard({
  scene,
  index,
  total,
  isDragging,
  isDropTarget,
  onPatch,
  onDuplicate,
  onDelete,
  onMove,
  onOpenLibrary,
  onDragStart,
  onDragOver,
  onDrop,
  onDragEnd,
}) {
  const shapeLabel = scene.shapeType === 'text'
    ? `"${scene.shapePayload}"`
    : scene.shapePayload;

  return (
    <div
      className={`scene-card${isDragging ? ' dragging' : ''}${isDropTarget ? ' drop-target' : ''}`}
      draggable
      onDragStart={(e) => {
        // Firefox refuses to start a drag unless data is set.
        e.dataTransfer.setData('text/plain', scene.id);
        e.dataTransfer.effectAllowed = 'move';
        onDragStart(index);
      }}
      onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; onDragOver(index); }}
      onDrop={(e) => { e.preventDefault(); onDrop(index); }}
      onDragEnd={onDragEnd}
    >
      <div className="scene-card-head">
        <span className="scene-grip" title="Drag to reorder"><GripVertical size={14} /></span>
        <span className="scene-index">{String(index + 1).padStart(2, '0')}</span>

        <input
          className="scene-name"
          value={scene.name}
          onChange={(e) => onPatch({ name: e.target.value })}
          aria-label={`Scene ${index + 1} name`}
        />

        <div className="scene-reorder">
          <button type="button" onClick={() => onMove(index, -1)} disabled={index === 0} title="Move up" aria-label="Move scene up">
            <ChevronUp size={12} />
          </button>
          <button type="button" onClick={() => onMove(index, 1)} disabled={index === total - 1} title="Move down" aria-label="Move scene down">
            <ChevronDown size={12} />
          </button>
        </div>

        <button type="button" className="scene-act" onClick={onDuplicate} title="Duplicate scene" aria-label="Duplicate scene">
          <Copy size={13} />
        </button>
        <button
          type="button"
          className="scene-act danger"
          onClick={onDelete}
          disabled={total <= 1}
          title={total <= 1 ? 'A show needs at least one scene' : 'Delete scene'}
          aria-label="Delete scene"
        >
          <Trash size={13} />
        </button>
      </div>

      <button type="button" className="scene-shape-btn" onClick={onOpenLibrary}>
        <Shapes size={13} />
        <span className="scene-shape-name">{shapeLabel}</span>
        <span className="scene-shape-scale">×{scene.scale.toFixed(2)}</span>
      </button>

      <div className="scene-card-times">
        <NumberField
          label="Hold" suffix="s" min={0} max={120} step={0.5}
          value={scene.holdDurationSeconds}
          onChange={(v) => onPatch({ holdDurationSeconds: v })}
        />
        <NumberField
          label="Morph in" suffix="s" min={0} max={60} step={0.5}
          value={scene.transitionDurationSeconds}
          onChange={(v) => onPatch({ transitionDurationSeconds: v })}
        />
        <NumberField
          label="Layer gap" suffix="m" min={0.5} max={6} step={0.1}
          value={scene.transitLayerGapMeters}
          onChange={(v) => onPatch({ transitLayerGapMeters: v })}
        />
      </div>
    </div>
  );
}
