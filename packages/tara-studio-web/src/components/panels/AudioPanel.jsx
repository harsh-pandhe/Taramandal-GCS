import { useRef } from 'react';
import { Music, X, RefreshCw, Zap, Sliders } from 'lucide-react';
import { AUDIO_ACCEPT } from '../../engine/audioEngine.js';
import LedTrackEditor from '../sequencer/LedTrackEditor.jsx';

const fmt = (s) => {
  if (!s) return '0:00';
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
};

/** Soundtrack loading, transport volume, timeline zoom, beat detection, and the LED track. */
export default function AudioPanel({
  audio, zoom, onZoom, ledTrack, onLedTrack, duration, currentTime, onScrub,
  onSnapScenes,
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
          {/* Issue #91: Music analysis section */}
          <div style={{ marginTop: '0.5rem', marginBottom: '0.75rem', display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
            {!audio.analysis ? (
              <button
                type="button"
                className="btn-secondary btn-inline"
                onClick={() => audio.analyzeTrack()}
                disabled={audio.analyzing}
              >
                <RefreshCw size={13} className={audio.analyzing ? 'animate-spin' : ''} />
                {audio.analyzing ? 'Analyzing Tempo & Rhythm…' : 'Analyze Beats & Tempo'}
              </button>
            ) : (
              <div style={{
                background: 'var(--bg-input)', border: '1px solid var(--border)',
                borderRadius: 'var(--r-sm)', padding: '0.5rem 0.6rem',
                display: 'flex', flexDirection: 'column', gap: '0.4rem',
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span style={{ fontSize: '0.7rem', color: 'var(--accent)', fontWeight: 600 }}>
                    🎵 {audio.analysis.bpm?.toFixed(1)} BPM
                  </span>
                  <span style={{ fontSize: '0.65rem', color: 'var(--text-dim)' }}>
                    {Math.round((audio.analysis.confidence || 0) * 100)}% conf
                  </span>
                </div>
                <div style={{ display: 'flex', gap: '0.8rem', fontSize: '0.65rem', color: 'var(--text-dim)' }}>
                  <span>Beats: {audio.analysis.beats?.length || 0}</span>
                  <span>Bars: {audio.analysis.bars?.times?.length || 0}</span>
                  {audio.analysis.sections?.length > 0 && (
                    <span>Sections: {audio.analysis.sections.length}</span>
                  )}
                </div>
                {onSnapScenes && (
                  <button
                    type="button"
                    className="btn-ghost btn-tiny"
                    style={{ marginTop: '0.2rem', justifyContent: 'center', borderColor: 'var(--border)' }}
                    onClick={onSnapScenes}
                    title="Snap scene boundaries to nearest musical beat/bar"
                  >
                    <Zap size={12} className="text-accent" /> Snap Scenes to Beats
                  </button>
                )}
              </div>
            )}
            {audio.analysisError && (
              <div className="access-error-msg" style={{ fontSize: '0.7rem' }}>
                {audio.analysisError}
              </div>
            )}
          </div>

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
