import { useRef } from 'react';
import {
  Compass, Globe, Users, Activity, AlertTriangle, Scan, RotateCcw, Maximize2, Minimize2,
} from 'lucide-react';
import SequencerHeader from '../sequencer/SequencerHeader.jsx';
import TimelineTrack from '../sequencer/TimelineTrack.jsx';
import WaveformTrack from '../sequencer/WaveformTrack.jsx';
import StageViewport3D from './StageViewport3D.jsx';
import ViewportControls from './ViewportControls.jsx';
import MatrixView from './MatrixView.jsx';
import { EmptyState, LoadingState } from '../common/index.jsx';

const VIEWS = [
  { id: '3d', icon: Compass, label: '3D STAGE' },
  { id: 'top-down', icon: Globe, label: 'TOP-DOWN' },
  { id: 'audience', icon: Users, label: 'AUDIENCE' },
  { id: 'matrix', icon: Activity, label: 'TELEMETRY' },
];

/** The director's stage: show readout, 3D preview, transport and timeline. */
export default function StagePage({
  header, viewMode, onViewMode, showPaths, onShowPaths,
  drawerOpen, onOpenDrawer, choreoData, currentTime, lightingOpts,
  dronesList, rate, loading, error, playback, segments, duration, audioPeaks, zoom,
  onScrub, onStep, activeSceneId, onSelectScene, projectId, cameraStore,
  fullscreen = false, onToggleFullscreen,
}) {
  const stageRef = useRef(null);
  // The telemetry matrix has no camera, and an empty stage has nothing to frame.
  const noCamera = viewMode === 'matrix' || !choreoData;

  return (
    <main className={`studio-viewport${fullscreen ? ' fullscreen-preview' : ''}`}>
      <SequencerHeader {...header} />

      <div className="view-mode-tabs-container">
        <div className="viewport-left-actions">
          <div className="view-mode-tabs">
            {VIEWS.map(({ id, icon: Icon, label }) => (
              <button
                key={id}
                type="button"
                className={`view-tab${viewMode === id ? ' active' : ''}`}
                onClick={() => onViewMode(id)}
              >
                <Icon size={13} /> {label}
              </button>
            ))}
          </div>

          <div className="view-mode-tabs">
            <button
              type="button" className="view-tab" disabled={noCamera}
              title="Frame the fleet where it is now (F)"
              onClick={() => stageRef.current?.fit()}
            >
              <Scan size={13} /> FIT
            </button>
            <button
              type="button" className="view-tab" disabled={noCamera}
              title="Back to the default view of the whole show (Home)"
              onClick={() => stageRef.current?.reset()}
            >
              <RotateCcw size={13} /> RESET
            </button>
          </div>
        </div>

        <div className="viewport-right-actions">
          <label className="toggle-inline">
            <input type="checkbox" checked={showPaths} onChange={(e) => onShowPaths(e.target.checked)} />
            Paths
          </label>
          {/* Issue #132: no console to reopen while the sidebar/console are
              both unmounted for full-screen preview. */}
          {!drawerOpen && !fullscreen && (
            <button type="button" className="floating-console-btn" onClick={onOpenDrawer}>
              <Activity size={14} /><span>CONSOLE</span>
            </button>
          )}
          {onToggleFullscreen && (
            <button
              type="button" className="view-tab fullscreen-toggle-btn" onClick={onToggleFullscreen}
              title={fullscreen ? 'Exit full-screen preview (Esc)' : 'Full-screen preview'}
            >
              {fullscreen ? <Minimize2 size={13} /> : <Maximize2 size={13} />}
              {fullscreen ? 'EXIT FULL-SCREEN' : 'FULL-SCREEN PREVIEW'}
            </button>
          )}
        </div>
      </div>

      <div className="viewport-stage">
        {viewMode === 'matrix' ? (
          <MatrixView
            choreoData={choreoData} currentTime={currentTime}
            dronesList={dronesList} rate={rate}
          />
        ) : (
          <StageViewport3D
            ref={stageRef} frameKey={projectId} cameraStore={cameraStore}
            choreoData={choreoData} currentTime={currentTime}
            viewMode={viewMode} lighting={lightingOpts} showPaths={showPaths}
          />
        )}

        {error && <div className="viewport-error"><AlertTriangle size={15} /> {error}</div>}
        {/* A rebuild (choreoData already present) leaves the existing preview
            on screen instead - the "BUILDING…" button label already says a
            build is running, and the current state is better than nothing. */}
        {!choreoData && loading && viewMode !== 'matrix' && (
          <div className="viewport-overlay">
            <LoadingState title="Building sequence…">
              Solving the drone assignment and compiling the trajectory.
            </LoadingState>
          </div>
        )}
        {!choreoData && !loading && viewMode !== 'matrix' && (
          <div className="viewport-overlay">
            <EmptyState icon={Compass} title="Stage empty">
              Add scenes in the console, then Generate Sequence.
            </EmptyState>
          </div>
        )}
      </div>

      <div className="timeline-overlay">
        <ViewportControls
          isPlaying={playback.isPlaying} onPlayPause={playback.togglePlay}
          onRewind={playback.rewind} onStep={onStep}
          isLooping={playback.isLooping} onToggleLoop={playback.toggleLoop}
          speed={playback.speed} onSpeed={playback.setSpeed}
          currentTime={currentTime} duration={duration}
        />
        <WaveformTrack
          peaks={audioPeaks} duration={duration} currentTime={currentTime}
          zoom={zoom} onScrub={onScrub}
        />

        <TimelineTrack
          segments={segments} duration={duration} currentTime={currentTime}
          onScrub={onScrub} onSelectScene={onSelectScene} activeSceneId={activeSceneId}
        />
      </div>
    </main>
  );
}
