// Screenshot baseline for every Studio page, console tab and modal (UI
// standard, step 0). Later migration steps are checked against these, so each
// shot is taken from a fixed, fully-loaded state:
//
//  - The backend is stubbed. The Studio calls /api/assign-chain and
//    /api/validate while building its demo show, and the Safety tab's battery
//    endurance section calls /api/validate-energy; fixtures keep the Safety
//    tab identical on every run without a live server.
//  - The 3D stage canvas is hidden. It's WebGL, and the UI migration changes
//    the chrome around it, not the scene inside it.
//  - Fonts are awaited, so a slow font load never shows up as a diff.
//  - The Project Vault (/api/projects, issue #101) is answered by an in-memory
//    copy of the backend API, empty at the start of each test. It lives here in
//    the test process, so it outlives a page reload like a real backend does.
import { fileURLToPath } from 'node:url';
import { test, expect } from '@playwright/test';
import { createFakeVaultServer } from '../src/engine/__fixtures__/fakeVaultServer.js';

const PASSPHRASE = 'TARAMANDAL_2026'; // src/components/SecurityGate.jsx

const PASSED_REPORT = {
  status: 'PASSED',
  summary: 'Choreography passed all safety checks!',
  metrics: { min_distance_m: 2.0, max_velocity_m_s: 3.16, max_acceleration_m_s2: 3.6, max_yaw_rate_deg_s: 0 },
  violations: { proximity: [], velocity: [], acceleration: [] },
};

const FAILED_REPORT = {
  status: 'FAILED',
  summary: 'Choreography failed safety validation checks.',
  metrics: { min_distance_m: 1.2, max_velocity_m_s: 4.8, max_acceleration_m_s2: 2.6, max_yaw_rate_deg_s: 45 },
  violations: {
    proximity: [
      { time: 6.1, drones: [2, 3], distance: 1.2 },
      { time: 6.2, drones: [2, 3], distance: 1.25 },
    ],
    velocity: [{ time: 12.4, drone: 4, velocity: 4.8 }],
    acceleration: [],
  },
};

// The shape tara_studio.energy.validate_energy returns, with a short fixed
// timeline so the battery chart draws the same curve every run.
const ENERGY_REPORT = {
  status: 'PASSED',
  summary: 'Battery endurance OK: worst drone ends with 95% remaining.',
  metrics: {
    total_energy_wh: 22.48, max_drone_energy_wh: 3.9, peak_current_a: 23.61, peak_current_drone: 5,
    peak_current_time_s: 5.1, peak_c_rate: 4.72, max_depth_of_discharge_pct: 4.6, min_remaining_pct: 95.4,
    worst_drone: 5, min_cell_voltage_v: 4.09, hover_power_w: 207.7, usable_capacity_mah: 5000,
    duration_s: 64.0, required_duration_reduction_s: 0, max_safe_duration_s: 64.0,
  },
  limits: { max_depth_of_discharge_pct: 70.0, max_c_rate: 25.0, min_cell_voltage_v: 3.5 },
  violations: { depth_of_discharge: [], c_rate: [], voltage: [] },
  timeline: {
    time_s: [0, 16, 32, 48, 64],
    min_remaining_pct: [100, 98.7, 97.5, 96.4, 95.4],
    mean_remaining_pct: [100, 98.9, 97.8, 96.8, 95.9],
    max_current_a: [23.61, 14.2, 14.1, 14.0, 8.3],
  },
};

async function stubBackend(page, report = PASSED_REPORT, vault = createFakeVaultServer()) {
  // Registered first because Playwright tries routes newest-first: the
  // specific stubs below must win over this catch-all.
  await page.route('**/api/**', (route) => route.fulfill({ status: 503, body: 'not stubbed in e2e' }));
  await page.route('**/api/assign-chain', async (route) => {
    // Identity permutation per formation: the naive mapping the Studio falls
    // back to anyway, answered instantly and identically every run.
    const { forms } = route.request().postDataJSON();
    const n = forms[0].length;
    await route.fulfill({ json: { target_idx: forms.map(() => Array.from({ length: n }, (_, i) => i)) } });
  });
  await page.route('**/api/validate', (route) => route.fulfill({ json: report }));
  await page.route('**/api/validate-energy', (route) => route.fulfill({ json: ENERGY_REPORT }));
  // Issue #49: a configured signing key, so the Transmit tab reads the same every run.
  await page.route('**/api/signing', (route) => route.fulfill({ json: SIGNING_READY }));
  await page.route(/\/api\/projects(\/[^?]*)?$/, async (route) => {
    if (vault.down) return route.abort('connectionrefused');
    const request = route.request();
    const { status, body } = vault.handle(
      request.method(), new URL(request.url()).pathname, request.postDataJSON() ?? undefined,
    );
    return route.fulfill(body === null ? { status } : { status, json: body });
  });
  return vault;
}

const SIGNING_READY = { configured: true, ready: true, key_id: 'ab12cd34ef567890', public_key: 'cHVibGljLWtleQ==' };

// The endurance section fetches on its own after a short debounce; shoot the
// Safety tab only once its verdict is on screen.
const enduranceSettled = (page) => expect(page.getByText(ENERGY_REPORT.summary)).toBeVisible();

async function openStudio(page, report, vault) {
  const stubbed = await stubBackend(page, report, vault);
  await page.goto('/');
  await page.getByPlaceholder(/authorization key/i).fill(PASSPHRASE);
  await page.getByRole('button', { name: 'AUTHORIZE CONNECTION' }).click();
  await expect(page.getByText(/^Built · /)).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  return stubbed;
}

const HIDE_STAGE_CSS = fileURLToPath(new URL('./screenshot.css', import.meta.url));

async function shot(page, name) {
  // Injected as an ordinary stylesheet: toHaveScreenshot's own style options
  // left the WebGL canvas visible.
  await page.addStyleTag({ path: HIDE_STAGE_CSS });
  await expect(page).toHaveScreenshot(`${name}.png`);
}
const consoleTab = (page, name) => page.locator('.drawer-tab-btn', { hasText: name }).click();

// A 3 s mono WAV with a loud tick every second over a quiet tone, built here so
// no binary fixture is committed and the waveform is identical every run.
function markerWav() {
  const rate = 8000, seconds = 3, samples = rate * seconds;
  const buf = Buffer.alloc(44 + samples * 2);
  buf.write('RIFF', 0); buf.writeUInt32LE(36 + samples * 2, 4); buf.write('WAVE', 8);
  buf.write('fmt ', 12); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(rate, 24); buf.writeUInt32LE(rate * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write('data', 36); buf.writeUInt32LE(samples * 2, 40);
  for (let i = 0; i < samples; i++) {
    const t = i / rate;
    const loud = t % 1 < 0.1;
    const v = (loud ? 0.9 : 0.05) * Math.sin(2 * Math.PI * (loud ? 1000 : 220) * t);
    buf.writeInt16LE(Math.round(v * 32767), 44 + i * 2);
  }
  return { name: 'markers-3s.wav', mimeType: 'audio/wav', buffer: buf };
}

test('security gate', async ({ page }) => {
  await stubBackend(page);
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'AUTHORIZE CONNECTION' })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await shot(page, 'gate');
});

test.describe('sequencer', () => {
  test.beforeEach(async ({ page }) => openStudio(page));

  test('sequence tab', async ({ page }) => {
    await shot(page, 'sequencer-sequence');
  });

  test('light tab', async ({ page }) => {
    await consoleTab(page, 'LIGHT');
    await shot(page, 'sequencer-light');
  });

  test('audio tab, empty', async ({ page }) => {
    await consoleTab(page, 'AUDIO');
    await shot(page, 'sequencer-audio-empty');
  });

  test('audio tab, track loaded', async ({ page }) => {
    await consoleTab(page, 'AUDIO');
    await page.locator('input[type="file"]').setInputFiles(markerWav());
    await expect(page.locator('.audio-name')).toHaveText('markers-3s.wav');
    await shot(page, 'sequencer-audio-loaded');
  });

  test('transmit tab', async ({ page }) => {
    await consoleTab(page, 'TRANSMIT');
    await expect(page.getByTestId('signing-status')).toBeVisible();
    await shot(page, 'sequencer-transmit');
  });

  test('safety tab, passed', async ({ page }) => {
    await consoleTab(page, 'SAFETY');
    await expect(page.getByText(PASSED_REPORT.summary)).toBeVisible();
    await enduranceSettled(page);
    await shot(page, 'sequencer-safety-passed');
  });

  test('console closed', async ({ page }) => {
    await page.getByRole('button', { name: 'Close console' }).click();
    await shot(page, 'sequencer-console-closed');
  });

  test('telemetry view', async ({ page }) => {
    await page.getByRole('button', { name: 'TELEMETRY' }).click();
    await shot(page, 'sequencer-telemetry');
  });

  test('shape library', async ({ page }) => {
    await page.locator('.scene-shape-btn').first().click();
    await expect(page.getByRole('dialog').or(page.locator('.modal-overlay, [class*="modal"]')).first()).toBeVisible();
    await shot(page, 'shape-library');
  });
});

test('safety tab, failed', async ({ page }) => {
  await openStudio(page, FAILED_REPORT);
  await consoleTab(page, 'SAFETY');
  await expect(page.getByText(FAILED_REPORT.summary)).toBeVisible();
  await enduranceSettled(page);
  await shot(page, 'sequencer-safety-failed');
});

test('project vault', async ({ page }) => {
  await openStudio(page);
  await page.getByRole('button', { name: 'Project Vault' }).click();
  await shot(page, 'project-vault');
});

test('swarm fleet', async ({ page }) => {
  await openStudio(page);
  await page.getByRole('button', { name: 'Swarm Fleet' }).click();
  await shot(page, 'swarm-fleet');
});

// Issue #100. Behaviour, not appearance: these drive the real history hook and
// keyboard listener, which the unit tests (no DOM) cannot reach.
test.describe('undo/redo', () => {
  test.beforeEach(async ({ page }) => openStudio(page));

  const cards = (page) => page.locator('.scene-card');
  const undo = (page) => page.getByRole('button', { name: 'Undo', exact: true });
  const redo = (page) => page.getByRole('button', { name: 'Redo', exact: true });
  // Focus something that is not a text field, where Ctrl+Z belongs to the app.
  const leaveTextField = (page) => page.locator('.section-subtitle').first().click();

  test('buttons undo and redo a scene edit', async ({ page }) => {
    await expect(cards(page)).toHaveCount(2);
    await expect(undo(page)).toBeDisabled();
    await expect(redo(page)).toBeDisabled();

    await page.getByRole('button', { name: 'Add Scene' }).click();
    await expect(cards(page)).toHaveCount(3);
    await expect(redo(page)).toBeDisabled();

    await undo(page).click();
    await expect(cards(page)).toHaveCount(2);
    await expect(undo(page)).toBeDisabled();

    await redo(page).click();
    await expect(cards(page)).toHaveCount(3);
    await expect(page.getByLabel('Scene 3 name')).toHaveValue('Scene 3');
  });

  test('keyboard shortcuts undo and redo', async ({ page }) => {
    await page.getByRole('button', { name: 'Add Scene' }).click();
    await page.getByRole('button', { name: 'Delete scene' }).first().click();
    await expect(cards(page)).toHaveCount(2);
    await expect(page.getByLabel('Scene 1 name')).toHaveValue('4');

    await leaveTextField(page);
    await page.keyboard.press('Control+z');
    await expect(cards(page)).toHaveCount(3);
    await expect(page.getByLabel('Scene 1 name')).toHaveValue('5');
    await page.keyboard.press('Control+z');
    await expect(cards(page)).toHaveCount(2);

    await page.keyboard.press('Control+Shift+z');
    await expect(cards(page)).toHaveCount(3);
    await page.keyboard.press('Control+y');
    await expect(cards(page)).toHaveCount(2);
    await expect(page.getByLabel('Scene 1 name')).toHaveValue('4');
  });

  test('typing a name is one undo step, and Ctrl+Z in the field is left to the browser', async ({ page }) => {
    const name = page.getByLabel('Scene 1 name');
    await name.click();
    await name.press('End');
    await name.pressSequentially('-intro');
    await expect(name).toHaveValue('5-intro');

    await page.locator('.history-list summary').click();
    await expect(page.locator('.history-list li')).toHaveText(['Opened', 'Edit scene name']);

    // The whole burst of keystrokes is a single step.
    await undo(page).click();
    await expect(name).toHaveValue('5');
    await redo(page).click();
    await expect(name).toHaveValue('5-intro');
    await expect(redo(page)).toBeDisabled();

    // With focus in the text field, Ctrl+Z is the browser's own text undo. Had
    // the app taken it, the edit would now be undone and Redo would light up.
    await name.focus();
    await page.keyboard.press('Control+z');
    await expect(redo(page)).toBeDisabled();
  });

  test('the history list jumps to any point', async ({ page }) => {
    await page.getByRole('button', { name: 'Add Scene' }).click();
    await page.getByRole('button', { name: 'Add Scene' }).click();
    await page.getByRole('button', { name: 'Duplicate scene' }).first().click();
    await expect(cards(page)).toHaveCount(5);

    await page.locator('.history-list summary').click();
    const entries = page.locator('.history-list li button');
    await expect(entries).toHaveText(['Opened', 'Add scene', 'Add scene', 'Duplicate scene "5"']);

    await entries.nth(1).click();
    await expect(cards(page)).toHaveCount(3);
    await expect(entries.nth(1)).toHaveClass(/current/);
    await expect(entries.nth(3)).toHaveClass(/undone/);

    await entries.nth(0).click();
    await expect(cards(page)).toHaveCount(2);

    await entries.nth(3).click();
    await expect(cards(page)).toHaveCount(5);
  });

  test('a new edit after undo discards the redo branch', async ({ page }) => {
    await page.getByRole('button', { name: 'Add Scene' }).click();
    await undo(page).click();
    await expect(redo(page)).toBeEnabled();

    await page.getByRole('button', { name: 'Duplicate scene' }).first().click();
    await expect(redo(page)).toBeDisabled();
    await expect(cards(page)).toHaveCount(3);
  });

  test('loading a project starts a fresh history', async ({ page }) => {
    await page.getByRole('button', { name: 'Add Scene' }).click();
    await expect(undo(page)).toBeEnabled();

    await page.getByRole('button', { name: 'Project Vault' }).click();
    await page.getByRole('button', { name: 'Load', exact: true }).first().click();
    await page.getByRole('button', { name: 'Sequencer', exact: true }).click();
    await expect(undo(page)).toBeDisabled();
    await expect(redo(page)).toBeDisabled();
  });
});

// Issue #100: a save no longer overwrites the previous one.
test('project save history can be browsed and restored', async ({ page }) => {
  await openStudio(page);
  const cards = page.locator('.scene-card');
  const save = page.getByRole('button', { name: 'Save Project' });

  await save.click();                                            // version 1: 2 scenes
  await page.getByRole('button', { name: 'Add Scene' }).click();
  await page.getByRole('button', { name: 'Add Scene' }).click();
  await expect(cards).toHaveCount(4);
  await save.click();                                            // version 2: 4 scenes
  await save.click();                                            // unchanged: no new version

  await page.getByRole('button', { name: 'Project Vault' }).click();
  const history = page.locator('.vault-versions');
  await expect(history.locator('summary')).toHaveText(/Save history \(2\)/);
  await history.locator('summary').click();
  const rows = history.locator('li');
  await expect(rows.nth(0)).toContainText('4 scenes');
  await expect(rows.nth(1)).toContainText('2 scenes');

  // Restore the older save: the editor reopens on it.
  await rows.nth(1).getByRole('button', { name: /^Restore / }).click();
  await expect(cards).toHaveCount(2);
  await expect(page.getByLabel('Scene 1 name')).toHaveValue('5');

  // Nothing was lost: the newer save is still listed and can be restored.
  await page.getByRole('button', { name: 'Project Vault' }).click();
  await history.locator('summary').click();
  await expect(rows.filter({ hasText: '4 scenes' })).toHaveCount(1);
  await rows.filter({ hasText: '4 scenes' }).getByRole('button', { name: /^Restore / }).click();
  await expect(cards).toHaveCount(4);

  // Unsaved edits are not lost by a restore: they are kept as a version first.
  await page.getByRole('button', { name: 'Delete scene' }).first().click();
  await expect(cards).toHaveCount(3);
  await page.getByRole('button', { name: 'Project Vault' }).click();
  await history.locator('summary').click();
  await rows.filter({ hasText: '2 scenes' }).getByRole('button', { name: /^Restore / }).click();
  await expect(cards).toHaveCount(2);
  await page.getByRole('button', { name: 'Project Vault' }).click();
  await history.locator('summary').click();
  await expect(rows.filter({ hasText: 'Before restore · 3 scenes' })).toHaveCount(1);

  // And the history survives a reload, since it is persisted with the project.
  await page.reload();
  await expect(page.getByText(/^Built · /)).toBeVisible();
  await page.getByRole('button', { name: 'Project Vault' }).click();
  await expect(page.locator('.vault-versions summary')).toHaveText(/Save history \(3\)/);
});

// Issue #101: projects are kept by the backend, not by this browser.
test.describe('backend project vault', () => {
  const cards = (page) => page.locator('.scene-card');
  const projectName = (page) => page.getByLabel('Project name');
  const vaultCards = (page) => page.locator('.vault-card');

  async function saveDiwali(page) {
    await projectName(page).fill('Diwali');
    await page.getByRole('button', { name: 'Add Scene' }).click();
    await expect(cards(page)).toHaveCount(3);
    await page.getByRole('button', { name: 'Generate Sequence' }).click();
    await expect(page.getByText(/^Built · /)).toBeVisible();
    await page.getByRole('button', { name: 'Save Project' }).click();
    await page.getByRole('button', { name: 'Project Vault' }).click();
    await expect(vaultCards(page).filter({ hasText: 'Diwali' })).toContainText('Scenes: 3');
    await expect(vaultCards(page).filter({ hasText: 'Diwali' })).toContainText('Trajectory built');
    await page.getByRole('button', { name: 'Sequencer', exact: true }).click();
  }

  test('a reload reopens the saved project instead of the demo show', async ({ page }) => {
    await openStudio(page);
    await saveDiwali(page);

    let builds = 0;
    page.on('request', (request) => { if (request.url().endsWith('/api/assign-chain')) builds += 1; });
    await page.reload();
    await expect(page.getByText(/^Built · /)).toBeVisible();
    await expect(projectName(page)).toHaveValue('Diwali');
    await expect(cards(page)).toHaveCount(3);
    // The built trajectory came back from the vault; nothing was rebuilt.
    expect(builds).toBe(0);
  });

  test('projects and their history survive a cleared browser cache', async ({ page }) => {
    await openStudio(page);
    await saveDiwali(page);

    await page.evaluate(() => { localStorage.clear(); sessionStorage.clear(); });
    await page.reload();
    await page.getByPlaceholder(/authorization key/i).fill(PASSPHRASE);
    await page.getByRole('button', { name: 'AUTHORIZE CONNECTION' }).click();
    await expect(page.getByText(/^Built · /)).toBeVisible();

    await expect(projectName(page)).toHaveValue('Diwali');
    await expect(cards(page)).toHaveCount(3);
    await page.getByRole('button', { name: 'Project Vault' }).click();
    // Two saves: leaving the name field saved the rename, then Save Project.
    await expect(page.locator('.vault-versions summary')).toHaveText(/Save history \(2\)/);
  });

  test('a project saved in one browser opens in another', async ({ page, browser }) => {
    const vault = await openStudio(page);
    await saveDiwali(page);

    const elsewhere = await browser.newContext();
    try {
      const other = await elsewhere.newPage();
      await openStudio(other, undefined, vault);
      await expect(projectName(other)).toHaveValue('Diwali');
      await expect(cards(other)).toHaveCount(3);
    } finally {
      await elsewhere.close();
    }
  });

  test('a save made elsewhere is kept, and this browser\'s edits become their own project', async ({ page, browser }) => {
    const vault = await openStudio(page);
    await saveDiwali(page);

    const elsewhere = await browser.newContext();
    try {
      const other = await elsewhere.newPage();
      await openStudio(other, undefined, vault);
      await other.getByRole('button', { name: 'Add Scene' }).click();
      await other.getByRole('button', { name: 'Save Project' }).click();
      await other.getByRole('button', { name: 'Project Vault' }).click();
      await expect(vaultCards(other).filter({ hasText: 'Diwali' })).toContainText('Scenes: 4');
    } finally {
      await elsewhere.close();
    }

    // This browser still has the 3-scene show open and does not know.
    await page.getByRole('button', { name: 'Delete scene' }).first().click();
    await page.getByRole('button', { name: 'Save Project' }).click();
    await expect(page.getByRole('alert')).toContainText('saved somewhere else');
    await expect(projectName(page)).toHaveValue('Diwali (conflicted copy)');

    await page.getByRole('button', { name: 'Project Vault' }).click();
    await expect(vaultCards(page)).toHaveCount(2);
    await expect(vaultCards(page).filter({ hasText: 'Diwali (conflicted copy)' })).toContainText('Scenes: 2');
    await expect(vaultCards(page).filter({ hasText: 'Scenes: 4' })).toHaveCount(1);
  });

  test('an unreachable vault is reported, and the show is saved once it is back', async ({ page }) => {
    const vault = createFakeVaultServer();
    vault.down = true;
    await openStudio(page, undefined, vault);          // the demo show still builds

    // A save that cannot be stored says so, right by the Save button.
    await page.getByRole('button', { name: 'Save Project' }).click();
    await expect(page.locator('.sidebar-project-info').getByRole('alert')).toContainText('cannot be reached');

    await page.getByRole('button', { name: 'Project Vault' }).click();
    await expect(page.getByRole('alert')).toContainText('cannot be reached');
    await expect(vaultCards(page)).toHaveCount(0);

    vault.down = false;
    await page.getByRole('button', { name: 'Retry' }).click();
    await expect(page.getByRole('alert')).toHaveCount(0);
    await expect(vaultCards(page)).toHaveCount(1);
    await expect(vaultCards(page)).toContainText('Scenes: 2');
  });

  test('projects saved in this browser before the vault are imported', async ({ page }) => {
    const scene = (id, name) => ({
      id, name, shapeType: 'text', shapePayload: name, scale: 1,
      holdDurationSeconds: 6, transitionDurationSeconds: 4, transitLayerGapMeters: 2,
    });
    const legacy = [{
      id: 'proj-1759400000000', name: 'Navratri', rate: 4, homeLat: 12.9716, homeLon: 77.5946,
      scenes: [scene('scene-a', 'A'), scene('scene-b', 'B'), scene('scene-c', 'C')],
      dronesList: Array.from({ length: 6 }, (_, i) => ({
        id: i, name: `Drone 0${i}`, hoverAlt: 2 + i * 0.7, spawnOffset: 2 * i, colorHex: '#38bdf8',
      })),
      choreoData: null, validation: null,
      versions: [{
        id: 'v-1759400000000-0', savedAt: '2026-10-02T10:13:20.000Z', label: 'Saved',
        scenes: [scene('scene-a', 'A')], rate: 4, homeLat: 12.9716, homeLon: 77.5946, dronesList: [],
      }],
    }];
    await page.addInitScript((projects) => {
      if (!localStorage.getItem('taramandal_projects')) localStorage.setItem('taramandal_projects', JSON.stringify(projects));
    }, legacy);

    const vault = await openStudio(page);
    await expect(projectName(page)).toHaveValue('Navratri');
    await expect(cards(page)).toHaveCount(3);
    expect(vault.stored('proj-1759400000000').versions).toHaveLength(1);

    // Imported once: a reload neither duplicates it nor loses the old copy.
    await page.reload();
    await expect(page.getByText(/^Built · /)).toBeVisible();
    await page.getByRole('button', { name: 'Project Vault' }).click();
    await expect(vaultCards(page)).toHaveCount(1);
    expect(await page.evaluate(() => JSON.parse(localStorage.getItem('taramandal_projects')).length)).toBe(1);
  });

  test('the sidebar shows an Unsaved indicator for live edits and clears it once saved (issue #132)', async ({ page }) => {
    const save = page.getByRole('button', { name: 'Save Project' });
    const unsaved = page.getByText('Unsaved');
    await openStudio(page);
    await expect(page.getByText(/^Built · /)).toBeVisible();

    // Establish a clean baseline explicitly, rather than assuming a cold
    // start on an empty vault is already clean - the demo show auto-builds
    // on startup, and that build itself genuinely isn't saved yet.
    await save.click();
    await expect(unsaved).toBeHidden();
    await expect(save).not.toHaveClass(/btn-primary/);

    await projectName(page).fill('Navratri');
    await expect(unsaved).toBeVisible();
    await expect(save).toHaveClass(/btn-primary/);

    await save.click();
    await expect(unsaved).toBeHidden();
    await expect(save).not.toHaveClass(/btn-primary/);

    // Adding a scene is a live edit too, not just the name field.
    await page.getByRole('button', { name: 'Add Scene' }).click();
    await expect(unsaved).toBeVisible();
  });
});

// Issue #102: fleet editing at 100 drones. Behaviour against the real page.
test.describe('bulk fleet editing', () => {
  const rows = (page) => page.locator('[data-testid="fleet-rows"] [role="row"]');
  const hoverOf = (page, n) => page.getByLabel(`Hover altitude of VEHICLE ${String(n).padStart(2, '0')}`);

  test.beforeEach(async ({ page }) => {
    await openStudio(page);
    await page.getByRole('button', { name: 'Swarm Fleet' }).click();
    await page.getByLabel(/Fleet size/).fill('100');
    await expect(page.getByTestId('fleet-count')).toContainText('100 vehicles');
  });

  test('100 drones keep the DOM bounded and the whole list reachable', async ({ page }) => {
    const mounted = await rows(page).count();
    expect(mounted).toBeGreaterThan(5);
    expect(mounted).toBeLessThanOrEqual(25);
    expect(await page.locator('input[type="color"]').count()).toBeLessThanOrEqual(26);

    // Scroll the whole list and record any main-thread stall while doing it.
    const longTasks = await page.evaluate(async () => {
      const stalls = [];
      const observer = new PerformanceObserver((list) => {
        for (const e of list.getEntries()) stalls.push(e.duration);
      });
      observer.observe({ type: 'longtask', buffered: false });
      const el = document.querySelector('[data-testid="fleet-rows"]');
      for (let y = 0; y <= el.scrollHeight; y += 200) {
        el.scrollTop = y;
        await new Promise(requestAnimationFrame);
      }
      observer.disconnect();
      return stalls;
    });
    expect(longTasks.filter((ms) => ms > 200)).toEqual([]);
    await expect(page.getByText('VEHICLE 99', { exact: true })).toBeVisible();
    expect(await rows(page).count()).toBeLessThanOrEqual(25);
  });

  test('search, select all shown, and set hover altitude in one action', async ({ page }) => {
    await page.getByLabel('Search vehicles').fill('drone 1');
    await expect(page.getByTestId('fleet-count')).toContainText('10 of 100 shown');
    await page.getByLabel('Select all shown').check();
    await expect(page.getByTestId('fleet-count')).toContainText('10 selected');

    await page.getByLabel('Bulk hover altitude').fill('15');
    await page.getByRole('group', { name: 'Edit selected vehicles' }).getByRole('button', { name: 'Apply' }).first().click();

    // All ten are in view while filtered; rows outside the window aren't mounted.
    for (const n of [10, 14, 19]) await expect(hoverOf(page, n)).toHaveValue('15');
    await page.getByLabel('Search vehicles').fill('');
    await expect(hoverOf(page, 10)).toHaveValue('15');
    await expect(hoverOf(page, 2)).toHaveValue('3.4');

    // One bulk edit is one undo step.
    await page.getByRole('button', { name: 'Undo', exact: true }).click();
    await expect(hoverOf(page, 10)).toHaveValue('9');
    await page.getByRole('button', { name: 'Redo', exact: true }).click();
    await expect(hoverOf(page, 10)).toHaveValue('15');
  });

  test('shift-click selects a range for a bulk colour change', async ({ page }) => {
    await page.getByLabel('Select VEHICLE 02').click();
    await page.getByLabel('Select VEHICLE 06').click({ modifiers: ['Shift'] });
    await expect(page.getByTestId('fleet-count')).toContainText('5 selected');

    await page.getByLabel('Bulk LED colour').fill('#ff0000');
    await page.getByRole('group', { name: 'Edit selected vehicles' }).getByRole('button', { name: 'Apply' }).nth(2).click();
    for (const n of [2, 3, 4, 5, 6]) {
      await expect(page.getByLabel(`LED colour of VEHICLE 0${n}`)).toHaveValue('#ff0000');
    }
    await expect(page.getByLabel('LED colour of VEHICLE 07')).not.toHaveValue('#ff0000');
  });
});

// Issue #49: signed export and transmit, against stubbed Studio and GCS backends.
test.describe('signed export and transmit', () => {
  const EXPORT = {
    filename: 'choreography.json', content: '{"drones": []}\n', mime_type: 'application/json', encoding: 'utf-8',
    signature: { filename: 'choreography.json.sig.json', content: '{"schema": "tara-show-signature/1"}\n', key_id: 'ab12cd34ef567890' },
  };

  test.beforeEach(async ({ page }) => {
    await openStudio(page);
    await page.route('**/api/export', (route) => route.fulfill({ json: EXPORT }));
  });

  test('the Transmit tab says which key signs exports', async ({ page }) => {
    await consoleTab(page, 'TRANSMIT');
    await expect(page.getByTestId('signing-status')).toContainText('signed with key ab12cd34ef567890');
  });

  test('without a usable key it warns that the GCS will refuse the show', async ({ page }) => {
    await page.route('**/api/signing', (route) => route.fulfill({
      json: { configured: false, ready: false, error: 'No signing key configured. Create one with `tara keygen`.' },
    }));
    // The panel asks for the status when the tab opens.
    await consoleTab(page, 'TRANSMIT');
    await expect(page.getByTestId('signing-status')).toContainText('not signed');
    await expect(page.getByTestId('signing-status')).toContainText('tara keygen');
  });

  test('a signed JSON export downloads the show and its manifest', async ({ page }) => {
    const downloads = [];
    page.on('download', (d) => downloads.push(d.suggestedFilename()));
    await consoleTab(page, 'TRANSMIT');
    await page.getByRole('button', { name: 'JSON' }).click();
    await expect.poll(() => downloads).toEqual(['choreography.json', 'choreography.json.sig.json']);
  });

  test('transmit sends the show, its signature and the GCS session token', async ({ page }) => {
    const uploads = [];
    await page.route('**/api/upload-trajectory', (route) => {
      const req = route.request();
      const auth = req.headers().authorization || null;
      uploads.push({ auth, body: req.postDataBuffer().toString('utf-8') });
      return auth
        ? route.fulfill({ json: { status: 'success', signature: { verified: true, signer: 'Studio laptop' } } })
        : route.fulfill({ status: 401, json: { detail: 'Not logged in.' } });
    });
    await page.route('**/api/auth/login', (route) => route.fulfill({
      json: { token: 'gcs-tok', operator: 'chloe', role: 'CHOREOGRAPHER', expires_at: 0 },
    }));
    page.on('dialog', (d) => d.accept());
    await consoleTab(page, 'TRANSMIT');
    const transmit = page.getByRole('button', { name: /Transmit to GCS Fleet/ });

    // Not signed in yet: the GCS says 401 and the Studio explains what to do.
    await transmit.click();
    await expect(page.getByText(/needs an operator sign-in/)).toBeVisible();

    await page.getByLabel('GCS operator').fill('chloe');
    await page.getByLabel('GCS passphrase').fill('choreographer passphrase');
    await page.getByRole('button', { name: 'Sign in to GCS' }).click();
    await expect(page.getByTestId('gcs-session')).toContainText('chloe · CHOREOGRAPHER');

    await transmit.click();
    await expect.poll(() => uploads.length).toBe(2);
    const sent = uploads[1];
    expect(sent.auth).toBe('Bearer gcs-tok');
    expect(sent.body).toContain('filename="choreography.json"');
    expect(sent.body).toContain('name="signature"; filename="choreography.json.sig.json"');
    expect(sent.body).toContain(EXPORT.content.trim());
    expect(sent.body).toContain(EXPORT.signature.content.trim());
  });
});

// Movable stage camera. The maths is unit-tested in engine/camera.test.js;
// these cover what only the component does. The canvas is WebGL, so the camera
// is read from the data-camera attribute the viewport keeps on its container.
test.describe('stage camera', () => {
  test.beforeEach(async ({ page }) => openStudio(page));

  const stage = (page) => page.locator('.canvas-container');
  const cameraOf = async (page) => JSON.parse(await stage(page).getAttribute('data-camera'));
  const view = (page, name) => page.getByRole('button', { name, exact: true }).click();

  // A spot on the stage clear of the view tabs, the console and the timeline.
  async function grip(page) {
    const box = await stage(page).boundingBox();
    return { x: box.x + box.width * 0.3, y: box.y + box.height * 0.45 };
  }
  async function drag(page, dx, dy, { button = 'left', shift = false } = {}) {
    const { x, y } = await grip(page);
    await page.mouse.move(x, y);
    if (shift) await page.keyboard.down('Shift');
    await page.mouse.down({ button });
    await page.mouse.move(x + dx, y + dy, { steps: 4 });
    await page.mouse.up({ button });
    if (shift) await page.keyboard.up('Shift');
  }
  async function wheel(page, deltaY) {
    const { x, y } = await grip(page);
    await page.mouse.move(x, y);
    await page.mouse.wheel(0, deltaY);
  }
  async function rebuild(page) {
    await page.getByRole('button', { name: 'Add Scene' }).click();
    await page.getByRole('button', { name: 'Generate Sequence' }).click();
    await expect(page.getByText(/^Built · /)).toBeVisible();
  }

  test('the 3D view rotates on a left drag and pans on a right or Shift drag', async ({ page }) => {
    const start = await cameraOf(page);

    await drag(page, 90, 30);
    const rotated = await cameraOf(page);
    expect(rotated.target).toEqual(start.target);
    expect(rotated.eye).not.toEqual(start.eye);

    await drag(page, 90, 30, { button: 'right' });
    const panned = await cameraOf(page);
    expect(panned.target).not.toEqual(rotated.target);

    await drag(page, -60, 20, { shift: true });
    expect((await cameraOf(page)).target).not.toEqual(panned.target);
  });

  test('a rebuild keeps the camera where the designer left it', async ({ page }) => {
    const framed = await cameraOf(page);
    await wheel(page, -300);
    await drag(page, 120, 40, { button: 'right' });
    const moved = await cameraOf(page);
    expect(moved).not.toEqual(framed);

    await rebuild(page);
    expect(await cameraOf(page)).toEqual(moved);
  });

  test('the camera survives leaving the stage and coming back', async ({ page }) => {
    await drag(page, 120, 40, { button: 'right' });
    const moved = await cameraOf(page);

    await view(page, 'TELEMETRY');
    await view(page, '3D STAGE');
    expect(await cameraOf(page)).toEqual(moved);

    await page.getByRole('button', { name: 'Project Vault' }).click();
    await page.getByRole('button', { name: 'Sequencer', exact: true }).click();
    expect(await cameraOf(page)).toEqual(moved);
  });

  test('a different project is framed afresh', async ({ page }) => {
    await wheel(page, 2000);
    await drag(page, 200, 80, { button: 'right' });
    const moved = await cameraOf(page);

    await page.getByRole('button', { name: 'Project Vault' }).click();
    await page.getByRole('button', { name: 'New Show' }).click();
    await page.getByRole('button', { name: 'Sequencer', exact: true }).click();
    await page.getByRole('button', { name: 'Generate Sequence' }).click();
    await expect(page.getByText(/^Built · /)).toBeVisible();
    expect(await cameraOf(page)).not.toEqual(moved);

    // Reset lands on the same automatic framing of the new project.
    const framed = await cameraOf(page);
    await page.getByRole('button', { name: 'RESET' }).click();
    expect(await cameraOf(page)).toEqual(framed);
  });

  test('Fit frames the fleet at the playhead, Reset returns to the whole show', async ({ page }) => {
    const framed = await cameraOf(page);
    const fitButton = page.getByRole('button', { name: 'FIT', exact: true });
    const resetButton = page.getByRole('button', { name: 'RESET', exact: true });

    // The playhead is at 0, where the fleet still sits on its pads.
    await fitButton.click();
    const fitted = await cameraOf(page);
    expect(fitted.target[1]).toBeLessThan(0.5);
    expect(fitted.target[1]).toBeLessThan(framed.target[1]);

    await drag(page, 90, 30);
    await resetButton.click();
    expect(await cameraOf(page)).toEqual(framed);

    // The same two actions from the keyboard; focus is on a button, not a field.
    await page.keyboard.press('f');
    expect(await cameraOf(page)).toEqual(fitted);
    await page.keyboard.press('Home');
    expect(await cameraOf(page)).toEqual(framed);
  });

  test('F typed into a text field is text, not a camera command', async ({ page }) => {
    const framed = await cameraOf(page);
    await page.getByLabel('Project name').fill('Fireflies');
    await page.getByLabel('Project name').press('f');
    await expect(page.getByLabel('Project name')).toHaveValue('Firefliesf');
    expect(await cameraOf(page)).toEqual(framed);
  });

  test('the top-down view pans on a drag and stays straight overhead', async ({ page }) => {
    await view(page, 'TOP-DOWN');
    const start = await cameraOf(page);
    await drag(page, 100, 50);
    const panned = await cameraOf(page);
    expect(panned.target[0]).not.toBe(start.target[0]);
    expect(panned.target[2]).not.toBe(start.target[2]);
    expect(panned.target[1]).toBe(start.target[1]);
    expect(panned.eye[0]).toBe(panned.target[0]);
    expect(panned.eye[1]).toBeGreaterThan(panned.target[1]);
  });

  test('the audience walks their line at eye height and never crosses to the far side', async ({ page }) => {
    await view(page, 'AUDIENCE');
    const start = await cameraOf(page);
    expect(start.eye[1]).toBe(1.7);

    await drag(page, 150, 0);
    const walked = await cameraOf(page);
    expect(walked.eye[2]).not.toBe(start.eye[2]);
    expect(walked.eye[0]).toBe(start.eye[0]);
    expect(walked.eye[1]).toBe(1.7);

    // Zoom in as far as it goes: still on the -X side, so text is not mirrored.
    await wheel(page, -100000);
    const close = await cameraOf(page);
    expect(close.eye[1]).toBe(1.7);
    expect(close.eye[0]).toBeLessThan(close.target[0]);
  });

  test('Fit and Reset are off where there is no camera', async ({ page }) => {
    const fitButton = page.getByRole('button', { name: 'FIT', exact: true });
    const resetButton = page.getByRole('button', { name: 'RESET', exact: true });
    await expect(fitButton).toBeEnabled();
    await expect(resetButton).toBeEnabled();
    await view(page, 'TELEMETRY');
    await expect(fitButton).toBeDisabled();
    await expect(resetButton).toBeDisabled();
  });

  test('full-screen preview hides the sidebar and console, keeps the camera, and Esc exits (issue #132)', async ({ page }) => {
    const enter = page.getByRole('button', { name: 'FULL-SCREEN PREVIEW' });
    const exit = page.getByRole('button', { name: 'EXIT FULL-SCREEN' });
    const sidebar = page.locator('.studio-sidebar');

    await expect(sidebar).toBeVisible();
    const before = await cameraOf(page);

    await enter.click();
    await expect(sidebar).toBeHidden();
    await expect(exit).toBeVisible();
    // Nothing to reopen a console drawer with - it isn't rendered either.
    await expect(page.locator('.control-drawer')).toHaveCount(0);

    const during = await cameraOf(page);
    expect(during).toEqual(before);

    await page.keyboard.press('Escape');
    await expect(sidebar).toBeVisible();
    await expect(enter).toBeVisible();

    const after = await cameraOf(page);
    expect(after).toEqual(before);
  });

  test('full-screen preview also exits via its own button, not just Escape', async ({ page }) => {
    await page.getByRole('button', { name: 'FULL-SCREEN PREVIEW' }).click();
    await expect(page.locator('.studio-sidebar')).toBeHidden();

    await page.getByRole('button', { name: 'EXIT FULL-SCREEN' }).click();
    await expect(page.locator('.studio-sidebar')).toBeVisible();
    await expect(page.getByRole('button', { name: 'FULL-SCREEN PREVIEW' })).toBeVisible();
  });
});
