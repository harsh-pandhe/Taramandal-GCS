import { useRef } from 'react';
import { Music, X } from 'lucide-react';
import { AUDIO_ACCEPT } from '../../engine/audioEngine.js';
import LedTrackEditor from '../sequencer/LedTrackEditor.jsx';

const fmt = (s) => {
  if (!s) return '0:00';
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
};

/** Soundtrack loading, transport volume, timeline zoom, and the LED track. */
export default function AudioPanel({
  audio, zoom, onZoom, ledTrack, onLedTrack, duration, currentTime, onScrub,
}) {
  const inputRef = useRef(null);

  return (
    <div className="glass-panel-inner">
      <h4 className="section-subtitle">Soundtrack</h4>

      <input
        ref={inputRef}
        type="file"
        accept={AUDIO_ACCEPT}
        className="visually-hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) audio.load(f);
          // Clear the input so picking the same file twice still fires change.
          e.target.value = '';
        }}
      />

      {!audio.hasTrack ? (
        <button
          type="button"
          className="btn-secondary btn-inline"
          onClick={() => inputRef.current?.click()}
          disabled={audio.loading}
        >
          <Music size={14} /> {audio.loading ? 'Decoding…' : 'Load Audio (.mp3/.wav/.ogg)'}
        </button>
      ) : (
        <div className="audio-loaded-row">
          <div className="audio-meta">
            <span className="mono audio-name" title={audio.name}>{audio.name}</span>
            <span className="telemetry-label">{fmt(audio.duration)}</span>
          </div>
          <button type="button" className="btn-ghost btn-tiny" onClick={audio.clear} title="Remove track">
            <X size={13} />
          </button>
        </div>
      )}

      {audio.error && <div className="access-error-msg">{audio.error}</div>}

      {audio.hasTrack && (
        <>
          <label className="ui-field">
            <span className="ui-field-label">Volume</span>
            <input
              type="range" min={0} max={1} step={0.01} value={audio.volume}
              onChange={(e) => audio.setVolume(Number(e.target.value))}
            />
          </label>

          <label className="ui-field">
            <span className="ui-field-label">Waveform Zoom</span>
            <div className="zoom-row">
              {[1, 2, 5].map((z) => (
                <button
                  key={z}
                  type="button"
                  className={`btn-ghost btn-inline${zoom === z ? ' active' : ''}`}
                  onClick={() => onZoom(z)}
                >
                  {z}x
                </button>
              ))}
            </div>
          </label>

          <p className="panel-hint">
            Playback follows the audio clock at 1x speed, so the stage stays in
            sync with the track. Other speeds fall back to the show clock and
            mute the soundtrack.
          </p>
        </>
      )}

      <LedTrackEditor
        track={ledTrack}
        onChange={onLedTrack}
        duration={duration}
        currentTime={currentTime}
        onScrub={onScrub}
      />
    </div>
  );
}
