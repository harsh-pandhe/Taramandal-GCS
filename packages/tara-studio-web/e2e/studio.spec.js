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
import { fileURLToPath } from 'node:url';
import { test, expect } from '@playwright/test';

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

async function stubBackend(page, report = PASSED_REPORT) {
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
}

// The endurance section fetches on its own after a short debounce; shoot the
// Safety tab only once its verdict is on screen.
const enduranceSettled = (page) => expect(page.getByText(ENERGY_REPORT.summary)).toBeVisible();

async function openStudio(page, report) {
  await stubBackend(page, report);
  await page.goto('/');
  await page.getByPlaceholder(/authorization key/i).fill(PASSPHRASE);
  await page.getByRole('button', { name: 'AUTHORIZE CONNECTION' }).click();
  await expect(page.getByText(/^Built · /)).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
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
