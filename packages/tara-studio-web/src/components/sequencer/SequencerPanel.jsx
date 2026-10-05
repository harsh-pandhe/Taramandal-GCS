import { Plus, Sparkles } from 'lucide-react';
import SceneCard from './SceneCard.jsx';
import HistoryBar from './HistoryBar.jsx';

/**
 * The scene list editor: the stack of scene cards plus the build action.
 */
export default function SequencerPanel({
  list, history, loading, onBuild, onOpenLibrary,
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

      <button type="button" className="btn-secondary btn-inline" onClick={() => addScene()}>
        <Plus size={14} /> Add Scene
      </button>

      <button
        type="button"
        className="btn-primary btn-inline"
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
