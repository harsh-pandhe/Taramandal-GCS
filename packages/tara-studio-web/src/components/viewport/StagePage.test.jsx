import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { createRef } from 'react';
import StagePage from './StagePage.jsx';

const noop = () => {};

const render = (props = {}) => renderToStaticMarkup(
  <StagePage
    header={{ sceneCount: 2, numDrones: 6, rate: 4, predictedDuration: 10, builtDuration: 0, stale: false }}
    viewMode="3d"
    onViewMode={noop}
    showPaths={true}
    onShowPaths={noop}
    drawerOpen={true}
    onOpenDrawer={noop}
    choreoData={null}
    projectId={null}
    cameraStore={createRef()}
    currentTime={0}
    lightingOpts={{}}
    dronesList={[]}
    rate={4}
    loading={false}
    error={null}
    playback={{ isPlaying: false, togglePlay: noop, rewind: noop, isLooping: false, toggleLoop: noop, speed: 1, setSpeed: noop }}
    audioPeaks={null}
    zoom={1}
    segments={[]}
    duration={10}
    onScrub={noop}
    onStep={noop}
    activeSceneId={null}
    onSelectScene={noop}
    {...props}
  />,
);

describe('StagePage loading state (issue #132)', () => {
  it('shows the empty state when there is no show and nothing is building', () => {
    const html = render({ choreoData: null, loading: false });
    expect(html).toContain('Stage empty');
    expect(html).not.toContain('Building sequence');
  });

  it('shows a loading state instead of the empty state while the first build runs', () => {
    const html = render({ choreoData: null, loading: true });
    expect(html).toContain('Building sequence');
    expect(html).not.toContain('Stage empty');
  });

  it('leaves the existing preview on screen during a rebuild, without either overlay', () => {
    const html = render({ choreoData: { drones: [] }, loading: true });
    expect(html).not.toContain('Building sequence');
    expect(html).not.toContain('Stage empty');
  });
});

describe('StagePage full-screen preview toggle (issue #132)', () => {
  it('offers "Full-Screen Preview" and no console button when not in fullscreen', () => {
    const html = render({ fullscreen: false, onToggleFullscreen: noop, drawerOpen: false });
    expect(html).toContain('FULL-SCREEN PREVIEW');
    expect(html).not.toContain('EXIT FULL-SCREEN');
    // drawerOpen=false would normally show the floating CONSOLE button too.
    expect(html).toContain('CONSOLE');
  });

  it('offers "Exit Full-Screen" and hides the console button while in fullscreen', () => {
    const html = render({ fullscreen: true, onToggleFullscreen: noop, drawerOpen: false });
    expect(html).toContain('fullscreen-preview');
    expect(html).toContain('EXIT FULL-SCREEN');
    expect(html).not.toContain('FULL-SCREEN PREVIEW');
    expect(html).not.toContain('CONSOLE');
  });

  it('omits the toggle entirely when the caller does not wire it up', () => {
    const html = render({ onToggleFullscreen: undefined });
    expect(html).not.toContain('FULL-SCREEN PREVIEW');
    expect(html).not.toContain('EXIT FULL-SCREEN');
  });
});
