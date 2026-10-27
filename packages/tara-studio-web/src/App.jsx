import { useState, useMemo, useEffect, useCallback, useRef } from 'react';

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

import {
  makeScene, computeShowDuration, buildTimelineSegments, MIN_CERTIFIABLE_RATE_HZ,
} from './engine/sequencer.js';
import { ShowRefusalError } from './engine/showErrors.js';
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
  // The stage camera, kept here because the stage unmounts on every page change.
  const stageCamera = useRef(null);
  const [librarySceneId, setLibrarySceneId] = useState(null);
  const [activeSceneId, setActiveSceneId] = useState(null);
  // issue #160: below tara_studio.validator.validate_dynamics' own fixed
  // 10 Hz sweep, the jerk certification measures a linear-interpolation
  // artifact between sparsely-spaced waypoints, not the curve's real jerk.
  const [rate, setRate] = useState(MIN_CERTIFIABLE_RATE_HZ);
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
  // Scenes are edited live, so this can run against a momentarily unsafe scene
  // list (too dense at the current fleet size, etc.) well before the user
  // hits Build. A refusal there just means "no prediction yet" — the Build
  // button surfaces the real message — but anything else is a genuine bug,
  // and must not be swallowed: this runs inside render, so an uncaught throw
  // here used to take the whole page down (issue #141).
  // issue #141 review: a refused prediction returning 0 is indistinguishable
  // in the UI from a real, tiny show - predictionRefused (and the message)
  // let SequencerHeader say "refused", not "0.0s", for exactly the
  // "no prediction yet" case this comment already described above.
  const prediction = useMemo(() => {
    try {
      return { duration: computeShowDuration(scenes, numDrones, rate, builtAssignment), refused: false, message: null };
    } catch (e) {
      if (e instanceof ShowRefusalError) return { duration: 0, refused: true, message: e.message };
      throw e;
    }
  }, [scenes, numDrones, rate, builtAssignment]);
  const predictedDuration = prediction.duration;
  const segments = useMemo(() => {
    try {
      return buildTimelineSegments(scenes, numDrones, rate, builtAssignment);
    } catch (e) {
      if (e instanceof ShowRefusalError) return [];
      throw e;
    }
  }, [scenes, numDrones, rate, builtAssignment]);
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

  // Build once on startup so the stage isn't empty - after the Project Vault
  // has put the last open project in the editor (issue #101), and only if that
  // project did not come with its built show. An unreachable vault still
  // boots, so the demo show builds either way.
  const builtOnStartup = useRef(false);
  useEffect(() => {
    if (!projects.booted || builtOnStartup.current) return;
    builtOnStartup.current = true;
    if (!choreoData) handleBuild();
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [projects.booted]);

  // Another browser may have saved since this one listed the vault.
  const refreshVault = projects.refresh;
  useEffect(() => { if (activePage === 'projects') refreshVault(); }, [activePage, refreshVault]);

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
    if (validation?.status !== 'PASSED') {
      const reason = validation?.status === 'FAILED'
        ? 'choreography failed safety validation checks. Fix violations before exporting'
        : 'choreography has not passed safety validation';
      builder.setError(`Export blocked: ${reason}.`);
      return;
    }
    try {
      downloadExport(await exportShow({
        choreo: buildColoredChoreo(choreoData, lightingOpts), format, homeLat, homeLon,
        ledTrack: ledTrack.length ? serializeTrack(ledTrack) : null,
        rateHz: rate,
      }));
    } catch (e) {
      builder.setError(`Export failed: ${e.message}`);
    }
  }, [choreoData, lightingOpts, homeLat, homeLon, builder, ledTrack, rate, validation]);

  // Issue #161: bypasses jerk certification only - no client-side gate on
  // validation status here, unlike handleExport. The backend still enforces
  // every hard limit and never signs a preview_only export regardless of a
  // configured key; TransmitPanel only shows this action when the normal
  // export is refused for jerk specifically, and reports whatever the
  // backend decides (including a refusal for an unrelated reason).
  const handleExportPreview = useCallback(async (format) => {
    if (!choreoData) return;
    downloadExport(await exportShow({
      choreo: buildColoredChoreo(choreoData, lightingOpts), format, homeLat, homeLon,
      ledTrack: ledTrack.length ? serializeTrack(ledTrack) : null,
      rateHz: rate,
      previewOnly: true,
    }));
  }, [choreoData, lightingOpts, homeLat, homeLon, ledTrack, rate]);

  const handleSendToGCS = useCallback(async () => {
    if (!choreoData) return;
    if (validation?.status !== 'PASSED') {
      const reason = validation?.status === 'FAILED'
        ? 'choreography failed safety validation checks. Fix violations before transmitting'
        : 'choreography has not passed safety validation';
      builder.setError(`GCS upload blocked: ${reason}.`);
      return;
    }
    setSending(true);
    try {
      await sendShowToGCS({
        choreo: buildColoredChoreo(choreoData, lightingOpts), homeLat, homeLon,
        ledTrack: ledTrack.length ? serializeTrack(ledTrack) : null,
        rateHz: rate,
      });
      builder.setError(null);
      alert('Trajectory pushed to the GCS fleet. Run launch checks on the GCS dashboard.');
    } catch (e) {
      builder.setError(`GCS upload failed: ${e.message}`);
    } finally {
      setSending(false);
    }
  }, [choreoData, lightingOpts, homeLat, homeLon, builder, ledTrack, rate, validation]);

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
          // The vault page shows it in full; everywhere else it goes by Save.
          vaultError={activePage === 'projects' ? null : projects.error}
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
              transmit={{
                onExport: handleExport, onExportPreview: handleExportPreview, onSendToGCS: handleSendToGCS, sending,
              }}
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
                predictionRefused: prediction.refused, refusalMessage: prediction.message,
              }}
              viewMode={viewMode}
              onViewMode={setViewMode}
              showPaths={showPaths}
              onShowPaths={setShowPaths}
              drawerOpen={drawerOpen}
              onOpenDrawer={() => setDrawerOpen(true)}
              choreoData={choreoData}
              projectId={projects.activeProjectId}
              cameraStore={stageCamera}
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
            status={projects.status}
            error={projects.error}
            onRetry={projects.retry}
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
