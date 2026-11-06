import { Plus, Sparkles, Image as ImageIcon } from 'lucide-react';
import SceneCard from './SceneCard.jsx';
import HistoryBar from './HistoryBar.jsx';
import { PACING_OPTIONS } from '../../engine/sequencer.js';

/**
 * The scene list editor: the stack of scene cards plus the build action.
 */
export default function SequencerPanel({
  list, history, loading, onBuild, onOpenLibrary, onOpenRasterImport, pacing = 'standard', onPacing,
}) {
  const {
    scenes, addScene, patchScene, duplicateScene, deleteScene, moveScene,
    dragIndex, dropIndex, onDragStart, onDragOver, onDrop, onDragEnd,
  } = list;

  return (
    <div className="glass-panel-inner">
      <h4 className="section-subtitle">Scene Sequencer</h4>
      <HistoryBar history={history} />

      <div className="scene-list">
        {scenes.map((scene, i) => (
          <SceneCard
            key={scene.id}
            scene={scene}
            index={i}
            total={scenes.length}
            isDragging={dragIndex === i}
            isDropTarget={dropIndex === i && dragIndex !== null && dragIndex !== i}
            onPatch={(patch) => patchScene(scene.id, patch)}
            onDuplicate={() => duplicateScene(scene.id)}
            onDelete={() => deleteScene(scene.id)}
            onMove={moveScene}
            onOpenLibrary={() => onOpenLibrary(scene.id)}
            onDragStart={onDragStart}
            onDragOver={onDragOver}
            onDrop={onDrop}
            onDragEnd={onDragEnd}
          />
        ))}
      </div>

      <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
        <button type="button" className="btn-secondary btn-inline" style={{ flex: 1 }} onClick={() => addScene()}>
          <Plus size={14} /> Add Scene
        </button>
        {onOpenRasterImport && (
          <button
            type="button"
            className="btn-secondary btn-inline"
            style={{ flex: 1 }}
            onClick={onOpenRasterImport}
            title="Import an image and convert it to a formation"
          >
            <ImageIcon size={14} /> Import Image
          </button>
        )}
      </div>

      <label className="ui-field">
        <span className="ui-field-label">Drone speed</span>
        <select value={pacing} onChange={(e) => onPacing && onPacing(e.target.value)} aria-label="Drone speed">
          {PACING_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </label>
      <p className="panel-hint">
        Standard keeps every move inside what PX4 flies as planned (climb under 3 m/s, descent under 1.5 m/s, gentle
        acceleration), so drones hold their spacing. The same safety validator checks the result. Generate Sequence again after changing it.
      </p>

      <button
        type="button"
        className="btn-primary btn-inline seq-generate"
        onClick={onBuild}
        disabled={loading || !scenes.length}
      >
        <Sparkles size={14} /> {loading ? 'BUILDING…' : 'Generate Sequence'}
      </button>

      <p className="panel-hint">
        The fleet takes off, morphs into each scene, holds it, then lands. Morphs
        are auto-paced to stay inside the velocity budget, so a very short morph
        across a large fleet will run longer than requested.
      </p>
    </div>
  );
}
