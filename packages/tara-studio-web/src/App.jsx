import { useState, useMemo, useEffect, useCallback } from 'react';

import SecurityGate from './components/SecurityGate.jsx';
import Sidebar from './components/Sidebar.jsx';
import ConsoleDrawer from './components/ConsoleDrawer.jsx';
import StagePage from './components/viewport/StagePage.jsx';
import ShapeLibraryModal from './components/modals/ShapeLibraryModal.jsx';
import FleetPage from './components/panels/FleetPage.jsx';
import VaultPage from './components/panels/VaultPage.jsx';

import useStudioHistory from './hooks/useStudioHistory.js';
import useSceneList from './hooks/useSceneList.js';
import usePlayback from './hooks/usePlayback.js';
import useFleet, { defaultFleet } from './hooks/useFleet.js';
import useShowBuilder from './hooks/useShowBuilder.js';
import useProjects from './hooks/useProjects.js';
import useAudioTrack from './hooks/useAudioTrack.js';

import { makeScene, computeShowDuration, buildTimelineSegments } from './engine/sequencer.js';
import { buildColoredChoreo } from './engine/lighting.js';
import { serializeTrack } from './engine/ledPatterns.js';
import { exportShow, sendShowToGCS, downloadExport } from './engine/api.js';
import './App.css';

const AUTH_DISABLED = import.meta.env.VITE_DISABLE_AUTH === 'true';

// The demo show from issue #15: "5" then "4", six seconds each.
const INITIAL_DOC = { scenes: [
  makeScene({ name: '5', shapeType: 'text', shapePayload: '5', holdDurationSeconds: 6, transitionDurationSeconds: 0 }),
  makeScene({ name: '4', shapeType: 'text', shapePayload: '4', holdDurationSeconds: 6, transitionDurationSeconds: 4 }),
], dronesList: defaultFleet(6) };

export default function App() {
  const [unlocked, setUnlocked] = useState(
    () => AUTH_DISABLED || sessionStorage.getItem('taramandal_unlocked') === 'true',
  );
  const [activePage, setActivePage] = useState('choreo');
  const [drawerOpen, setDrawerOpen] = useState(true);
  const [drawerTab, setDrawerTab] = useState('sequence');
  const [viewMode, setViewMode] = useState('3d');
  const [librarySceneId, setLibrarySceneId] = useState(null);
  const [activeSceneId, setActiveSceneId] = useState(null);
  const [rate, setRate] = useState(4);
  const [showPaths, setShowPaths] = useState(true);
  const [lightEffect, setLightEffect] = useState('perDrone');
  const [lightColor, setLightColor] = useState('#38bdf8');
  const [homeLat, setHomeLat] = useState(12.9716);
  const [homeLon, setHomeLon] = useState(77.5946);
  const [sending, setSending] = useState(false);
  const [ledTrack, setLedTrack] = useState([]);
  const [zoom, setZoom] = useState(1);

  const audio = useAudioTrack();
  const history = useStudioHistory(INITIAL_DOC);
  const list = useSceneList(history);
  const fleet = useFleet(history);
  const builder = useShowBuilder();

  const { scenes } = list;
  const { choreoData, validation, loading, error } = builder;
  const numDrones = fleet.dronesList.length;

  // Predicted length of the *current* scene list; the built show may be older.
  // Leg durations depend on which drone flies to which point, so predict with
  // the built show's assignment — for an unchanged scene list that makes the
  // prediction match the build exactly, and any edit shows up as stale.
  const builtAssignment = choreoData?._targetIdx ?? null;
  const predictedDuration = useMemo(
    () => computeShowDuration(scenes, numDrones, rate, builtAssignment),
    [scenes, numDrones, rate, builtAssignment],
  );
  const segments = useMemo(
    () => buildTimelineSegments(scenes, numDrones, rate, builtAssignment),
    [scenes, numDrones, rate, builtAssignment],
  );
  const builtDuration = choreoData?._duration ?? 0;
  // Scrub against what's actually rendered, so the playhead can never point past
  // the built trajectory.
  const showDuration = builtDuration || predictedDuration;
  const timelineDuration = Math.max(showDuration, audio.duration);
  const stale = !!choreoData && Math.abs(builtDuration - predictedDuration) > 0.01;

  const playback = usePlayback(timelineDuration, audio.hasTrack ? audio.clock : null);
  const { currentTime, scrub, setIsPlaying } = playback;

  const lightingOpts = useMemo(() => ({
    effect: lightEffect, baseColor: lightColor, dronesList: fleet.dronesList,
    numDrones, duration: timelineDuration || 1, audioVolume: 0,
    ledTrack: ledTrack.length ? ledTrack : null,
  }), [lightEffect, lightColor, fleet.dronesList, numDrones, timelineDuration, ledTrack]);

  const projects = useProjects({
    scenes, setScenes: list.setScenes, rate, setRate,
    dronesList: fleet.dronesList, setDronesList: fleet.setDronesList,
    homeLat, setHomeLat, homeLon, setHomeLon,
    choreoData, setChoreoData: builder.setChoreoData,
    validation, setValidation: builder.setValidation,
  });

  const handleBuild = useCallback(async () => {
    const show = await builder.build(scenes, numDrones, rate);
    if (show) { setIsPlaying(false); scrub(0); }
  }, [builder, scenes, numDrones, rate, setIsPlaying, scrub]);

  // Build the demo show once so the stage isn't empty on first load.
  useEffect(() => { handleBuild(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  // Jump the playhead to the previous/next scene boundary.
  const stepScene = useCallback((dir) => {
    const marks = [0, ...segments.map((s) => s.end)];
    const next = dir > 0
      ? marks.find((m) => m > currentTime + 1e-3)
      : [...marks].reverse().find((m) => m < currentTime - 1e-3);
    if (next !== undefined) scrub(next);
  }, [segments, currentTime, scrub]);

  const handleExport = useCallback(async (format) => {
    if (!choreoData) return;
    try {
      downloadExport(await exportShow({
        choreo: buildColoredChoreo(choreoData, lightingOpts), format, homeLat, homeLon,
        ledTrack: ledTrack.length ? serializeTrack(ledTrack) : null,
        rateHz: rate,
      }));
    } catch (e) {
      builder.setError(`Export failed: ${e.message}`);
    }
  }, [choreoData, lightingOpts, homeLat, homeLon, builder, ledTrack, rate]);

  const handleSendToGCS = useCallback(async () => {
    if (!choreoData) return;
    setSending(true);
    try {
      await sendShowToGCS({
        choreo: buildColoredChoreo(choreoData, lightingOpts), homeLat, homeLon,
      });
      builder.setError(null);
      alert('Trajectory pushed to the GCS fleet. Run launch checks on the GCS dashboard.');
    } catch (e) {
      builder.setError(`GCS upload failed: ${e.message}`);
    } finally {
      setSending(false);
    }
  }, [choreoData, lightingOpts, homeLat, homeLon, builder]);

  if (!unlocked) return <SecurityGate onUnlock={() => setUnlocked(true)} />;

  const libraryScene = scenes.find((s) => s.id === librarySceneId);

  return (
    <div className="studio-container">
      <div className="studio-workspace">
        <Sidebar
          activePage={activePage}
          onNavigate={setActivePage}
          projectName={projects.projectName}
          onProjectName={projects.setProjectName}
          onSave={projects.saveCurrent}
        />

        {activePage === 'choreo' && (
          <>
            <ConsoleDrawer
              open={drawerOpen}
              onClose={() => setDrawerOpen(false)}
              tab={drawerTab}
              onTab={setDrawerTab}
              list={list}
              history={history}
              loading={loading}
              onBuild={handleBuild}
              onOpenLibrary={setLibrarySceneId}
              light={{
                effect: lightEffect, onEffect: setLightEffect,
                baseColor: lightColor, onBaseColor: setLightColor,
              }}
              choreoData={choreoData}
              currentTime={currentTime}
              lightingOpts={lightingOpts}
              validation={validation}
              venue={{ homeLat, homeLon, onHomeLat: setHomeLat, onHomeLon: setHomeLon }}
              transmit={{ onExport: handleExport, onSendToGCS: handleSendToGCS, sending }}
              audio={{
                track: audio, zoom, onZoom: setZoom,
                ledTrack, onLedTrack: setLedTrack,
                duration: timelineDuration,
                onScrub: (t) => { setIsPlaying(false); scrub(t); },
              }}
            />

            <StagePage
              header={{
                sceneCount: scenes.length, numDrones, rate,
                predictedDuration, builtDuration, stale,
              }}
              viewMode={viewMode}
              onViewMode={setViewMode}
              showPaths={showPaths}
              onShowPaths={setShowPaths}
              drawerOpen={drawerOpen}
              onOpenDrawer={() => setDrawerOpen(true)}
              choreoData={choreoData}
              currentTime={currentTime}
              lightingOpts={lightingOpts}
              dronesList={fleet.dronesList}
              rate={rate}
              loading={loading}
              error={error}
              playback={playback}
              audioPeaks={audio.peaks}
              zoom={zoom}
              segments={segments}
              duration={timelineDuration}
              onScrub={(t) => { setIsPlaying(false); scrub(t); }}
              onStep={stepScene}
              activeSceneId={activeSceneId}
              onSelectScene={setActiveSceneId}
            />
          </>
        )}

        {activePage === 'projects' && (
          <VaultPage
            projectsList={projects.projectsList}
            activeProjectId={projects.activeProjectId}
            onLoad={projects.load}
            onCreate={projects.create}
            onDelete={projects.remove}
            onRestore={(id, versionId) => { projects.restore(id, versionId); setActivePage('choreo'); }}
          />
        )}

        {activePage === 'fleet' && (
          <FleetPage
            dronesList={fleet.dronesList}
            onAdd={fleet.addDrone}
            onRemove={fleet.removeDrone}
            onUpdate={fleet.updateDrone}
            onBulkUpdate={fleet.bulkUpdate}
            onSetCount={fleet.setCount}
            history={history}
          />
        )}
      </div>

      {libraryScene && (
        <ShapeLibraryModal
          scene={libraryScene}
          numDrones={numDrones}
          onClose={() => setLibrarySceneId(null)}
          onApply={(patch) => { list.patchScene(libraryScene.id, patch); setLibrarySceneId(null); }}
        />
      )}
    </div>
  );
}
