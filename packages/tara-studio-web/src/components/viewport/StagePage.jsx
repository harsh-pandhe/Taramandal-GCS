import {
  Compass, Globe, Users, Activity, AlertTriangle,
} from 'lucide-react';
import SequencerHeader from '../sequencer/SequencerHeader.jsx';
import TimelineTrack from '../sequencer/TimelineTrack.jsx';
import WaveformTrack from '../sequencer/WaveformTrack.jsx';
import StageViewport3D from './StageViewport3D.jsx';
import ViewportControls from './ViewportControls.jsx';
import MatrixView from './MatrixView.jsx';
import { EmptyState } from '../common/index.jsx';

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
  onScrub, onStep, activeSceneId, onSelectScene,
}) {
  return (
    <main className="studio-viewport">
      <SequencerHeader {...header} />

      <div className="view-mode-tabs-container">
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

        <div className="viewport-right-actions">
          <label className="toggle-inline">
            <input type="checkbox" checked={showPaths} onChange={(e) => onShowPaths(e.target.checked)} />
            Paths
          </label>
          {!drawerOpen && (
            <button type="button" className="floating-console-btn" onClick={onOpenDrawer}>
              <Activity size={14} /><span>CONSOLE</span>
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
            choreoData={choreoData} currentTime={currentTime}
            viewMode={viewMode} lighting={lightingOpts} showPaths={showPaths}
          />
        )}

        {error && <div className="viewport-error"><AlertTriangle size={15} /> {error}</div>}
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
