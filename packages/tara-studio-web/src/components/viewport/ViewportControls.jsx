import { Play, Pause, RotateCcw, Repeat, SkipBack, SkipForward } from 'lucide-react';
import { IconButton } from '../common/index.jsx';

const SPEEDS = [1, 2, 4];

/**
 * Transport controls for the preview: play/pause, rewind, step between scene
 * boundaries, loop, and playback speed.
 */
export default function ViewportControls({
  isPlaying, onPlayPause, onRewind, onStep,
  isLooping, onToggleLoop, speed, onSpeed,
  currentTime, duration,
}) {
  return (
    <div className="viewport-controls">
      <IconButton
        icon={isPlaying ? Pause : Play}
        label={isPlaying ? 'Pause' : 'Play'}
        onClick={onPlayPause}
        size={16}
        tone="primary"
      />
      <IconButton icon={RotateCcw} label="Rewind to start" onClick={onRewind} />
      <IconButton icon={SkipBack} label="Previous scene" onClick={() => onStep(-1)} />
      <IconButton icon={SkipForward} label="Next scene" onClick={() => onStep(1)} />
      <IconButton
        icon={Repeat}
        label={isLooping ? 'Looping on' : 'Looping off'}
        active={isLooping}
        onClick={onToggleLoop}
      />

      <span className="vc-time">
        {currentTime.toFixed(2)}s <em>/ {duration.toFixed(2)}s</em>
      </span>

      <div className="vc-speeds" role="group" aria-label="Playback speed">
        {SPEEDS.map((s) => (
          <button
            type="button"
            key={s}
            className={`vc-speed${speed === s ? ' active' : ''}`}
            aria-pressed={speed === s}
            onClick={() => onSpeed(s)}
          >
            {s}x
          </button>
        ))}
      </div>
    </div>
  );
}
