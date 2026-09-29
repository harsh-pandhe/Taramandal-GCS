// Screenshot baseline for every Studio page, console tab and modal (UI
// standard, step 0). Later migration steps are checked against these, so each
// shot is taken from a fixed, fully-loaded state:
//
//  - The backend is stubbed. The Studio calls /api/assign-chain and
//    /api/validate while building its demo show; fixtures keep the Safety tab
//    identical on every run without a live server.
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
}

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
