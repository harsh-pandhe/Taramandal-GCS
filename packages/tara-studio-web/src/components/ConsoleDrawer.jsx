import { X } from 'lucide-react';
import SequencerPanel from './sequencer/SequencerPanel.jsx';
import LightPanel from './panels/LightPanel.jsx';
import SafetyPanel from './panels/SafetyPanel.jsx';
import TransmitPanel from './panels/TransmitPanel.jsx';

const TABS = ['sequence', 'light', 'safety', 'transmit'];

/** The sliding control console: scene editing, lighting, safety, export. */
export default function ConsoleDrawer({
  open, onClose, tab, onTab, list, loading, onBuild, onOpenLibrary,
  light, choreoData, currentTime, lightingOpts, validation, venue, transmit,
}) {
  return (
    <div className={`control-drawer${open ? ' active' : ''}`}>
      <div className="drawer-header">
        <h3>CONTROL CONSOLE</h3>
        <button type="button" className="close-drawer-btn" onClick={onClose} aria-label="Close console">
          <X size={16} />
        </button>
      </div>

      <div className="drawer-tabs">
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            className={`drawer-tab-btn${tab === t ? ' active' : ''}`}
            onClick={() => onTab(t)}
          >
            {t.toUpperCase()}
          </button>
        ))}
      </div>

      <div className="drawer-content">
        {tab === 'sequence' && (
          <SequencerPanel
            list={list} loading={loading}
            onBuild={onBuild} onOpenLibrary={onOpenLibrary}
          />
        )}
        {tab === 'light' && (
          <LightPanel
            effect={light.effect} onEffect={light.onEffect}
            baseColor={light.baseColor} onBaseColor={light.onBaseColor}
            choreoData={choreoData} currentTime={currentTime} lightingOpts={lightingOpts}
          />
        )}
        {tab === 'safety' && <SafetyPanel validation={validation} />}
        {tab === 'transmit' && (
          <TransmitPanel
            homeLat={venue.homeLat} homeLon={venue.homeLon}
            onHomeLat={venue.onHomeLat} onHomeLon={venue.onHomeLon}
            onExport={transmit.onExport} onSendToGCS={transmit.onSendToGCS}
            sending={transmit.sending} hasShow={!!choreoData}
          />
        )}
      </div>
    </div>
  );
}
