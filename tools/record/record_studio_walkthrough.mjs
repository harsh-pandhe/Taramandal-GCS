import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execSync } from 'node:child_process';
import { chromium } from '../../packages/tara-studio-web/node_modules/@playwright/test/index.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, '../..');
const distDir = path.resolve(rootDir, 'packages/tara-studio-web/dist');
const recordingsDir = path.resolve(rootDir, 'docs/recordings');
const tempDir = path.resolve(recordingsDir, 'temp');

fs.mkdirSync(recordingsDir, { recursive: true });
fs.mkdirSync(tempDir, { recursive: true });

// Ensure dist exists
if (!fs.existsSync(path.join(distDir, 'index.html'))) {
  console.log('Building tara-studio-web...');
  execSync('npm --prefix packages/tara-studio-web run build', { cwd: rootDir, stdio: 'inherit' });
}

// 1. Lightweight in-memory static server
const mimeTypes = {
  '.html': 'text/html',
  '.js': 'application/javascript',
  '.css': 'text/css',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.json': 'application/json',
  '.woff2': 'font/woff2',
};

const server = http.createServer((req, res) => {
  let reqPath = req.url.split('?')[0];
  if (reqPath === '/') reqPath = '/index.html';
  let fullPath = path.join(distDir, reqPath);
  if (!fs.existsSync(fullPath)) fullPath = path.join(distDir, 'index.html');
  const ext = path.extname(fullPath);
  res.writeHead(200, { 'Content-Type': mimeTypes[ext] || 'application/octet-stream' });
  fs.createReadStream(fullPath).pipe(res);
});

// Helper: Virtual cursor injection & control
const VIRTUAL_CURSOR_INJECT = `
(() => {
  if (document.getElementById('__virtual_cursor__')) return;
  const cursor = document.createElement('div');
  cursor.id = '__virtual_cursor__';
  cursor.innerHTML = \`
    <div style="position: relative;">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" style="filter: drop-shadow(0 2px 5px rgba(0,0,0,0.6));">
        <path d="M5.5 3.2L18.5 13.5L12 14.5L14.5 20.8L11.5 22L9 15.8L5.5 19.3V3.2Z" fill="#38bdf8" stroke="#0f172a" stroke-width="1.5" stroke-linejoin="round"/>
      </svg>
      <div id="__cursor_ripple__" style="position: absolute; top: 0; left: 0; width: 30px; height: 30px; border-radius: 50%; border: 2px solid #38bdf8; background: rgba(56,189,248,0.3); pointer-events: none; opacity: 0; transform: translate(-10px, -10px) scale(0.2); transition: transform 0.4s ease-out, opacity 0.4s ease-out;"></div>
    </div>
  \`;
  cursor.style.position = 'fixed';
  cursor.style.top = '0px';
  cursor.style.left = '0px';
  cursor.style.pointerEvents = 'none';
  cursor.style.zIndex = '2147483647';
  cursor.style.transform = 'translate(100px, 100px)';
  cursor.style.transition = 'transform 0.04s linear';
  document.body.appendChild(cursor);

  window.__cursorState = { x: 100, y: 100 };
  window.__updateCursor = (x, y) => {
    window.__cursorState = { x, y };
    cursor.style.transform = \`translate(\${x}px, \${y}px)\`;
  };
  window.__clickRipple = () => {
    const rip = document.getElementById('__cursor_ripple__');
    if (!rip) return;
    rip.style.transition = 'none';
    rip.style.opacity = '1';
    rip.style.transform = 'translate(-10px, -10px) scale(0.2)';
    requestAnimationFrame(() => {
      rip.style.transition = 'transform 0.45s cubic-bezier(0.2, 0.8, 0.2, 1), opacity 0.45s ease-out';
      rip.style.transform = 'translate(-10px, -10px) scale(1.6)';
      rip.style.opacity = '0';
    });
  };
})();
`;

async function main() {
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const baseUrl = `http://127.0.0.1:${port}`;
  console.log(`[Recorder] Preview server live at ${baseUrl}`);

  const browser = await chromium.launch({
    headless: true,
    args: [
      '--enable-webgl',
      '--use-gl=angle',
      '--use-angle=swiftshader',
      '--no-sandbox',
      '--disable-setuid-sandbox',
    ],
  });

  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    recordVideo: {
      dir: tempDir,
      size: { width: 1440, height: 900 },
    },
  });

  const page = await context.newPage();

  // Stub API endpoints for stable, rich UI demo
  await page.route('**/api/assign-chain', async (route) => {
    const postData = route.request().postDataJSON() || {};
    const forms = postData.forms || [];
    const n = forms[0]?.length || 6;
    await route.fulfill({
      status: 200,
      json: { target_idx: forms.map(() => Array.from({ length: n }, (_, i) => i)) },
    });
  });

  await page.route('**/api/validate', (route) => route.fulfill({
    status: 200,
    json: {
      status: 'PASSED',
      summary: 'Choreography passed all safety checks!',
      metrics: { min_distance_m: 2.15, max_velocity_m_s: 3.16, max_acceleration_m_s2: 3.6, max_yaw_rate_deg_s: 0 },
      violations: { proximity: [], velocity: [], acceleration: [] },
    },
  }));

  await page.route('**/api/validate-energy', (route) => route.fulfill({
    status: 200,
    json: {
      status: 'PASSED',
      summary: 'Battery endurance OK: worst drone ends with 95.4% remaining.',
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
    },
  }));

  await page.route('**/api/signing', (route) => route.fulfill({
    status: 200,
    json: { configured: true, ready: true, key_id: 'ab12cd34ef567890', public_key: 'cHVibGljLWtleQ==' },
  }));

  let currentCursor = { x: 720, y: 450 };

  async function smoothMoveTo(x, y, durationMs = 450) {
    const startX = currentCursor.x;
    const startY = currentCursor.y;
    const steps = Math.max(10, Math.floor(durationMs / 25));
    const stepDelay = durationMs / steps;

    for (let i = 1; i <= steps; i++) {
      const t = i / steps;
      const ease = 0.5 - 0.5 * Math.cos(Math.PI * t);
      const curX = Math.round(startX + (x - startX) * ease);
      const curY = Math.round(startY + (y - startY) * ease);
      await page.mouse.move(curX, curY);
      await page.evaluate(({ curX, curY }) => window.__updateCursor?.(curX, curY), { curX, curY }).catch(() => {});
      await page.waitForTimeout(stepDelay);
    }
    currentCursor = { x, y };
  }

  async function clickPoint(x, y) {
    await smoothMoveTo(x, y, 400);
    await page.waitForTimeout(60);
    await page.evaluate(() => window.__clickRipple?.()).catch(() => {});
    await page.mouse.down();
    await page.waitForTimeout(60);
    await page.mouse.up();
    await page.waitForTimeout(180);
  }

  async function clickLocator(locator, offsetX = 0.5, offsetY = 0.5) {
    try {
      const box = await locator.boundingBox({ timeout: 5000 });
      if (!box) {
        console.warn('Could not find bounding box for click');
        return;
      }
      const targetX = Math.round(box.x + box.width * offsetX);
      const targetY = Math.round(box.y + box.height * offsetY);
      await clickPoint(targetX, targetY);
    } catch (e) {
      console.warn('Click locator skipped:', e.message);
    }
  }

  console.log('[Recorder] Navigating to Taramandal Studio...');
  await page.goto(baseUrl);
  await page.waitForLoadState('networkidle');
  await page.evaluate(VIRTUAL_CURSOR_INJECT);

  // 1. Authorization Gate walkthrough
  console.log('[Recorder] Step 1: Entering authorization key...');
  const keyInput = page.getByPlaceholder(/authorization key/i);
  if (await keyInput.isVisible().catch(() => false)) {
    await clickLocator(keyInput);
    await page.keyboard.type('TARAMANDAL_2026', { delay: 40 });
    await page.waitForTimeout(200);

    const authBtn = page.getByRole('button', { name: 'AUTHORIZE CONNECTION' });
    await clickLocator(authBtn);
    await page.waitForTimeout(1000);
  }

  await page.evaluate(VIRTUAL_CURSOR_INJECT);
  await page.waitForTimeout(500);

  // 2. Generate Sequence to build the choreography
  console.log('[Recorder] Step 2: Generating choreography sequence...');
  const genBtn = page.locator('button.seq-generate');
  if (await genBtn.isVisible().catch(() => false)) {
    await clickLocator(genBtn);
    await page.waitForTimeout(1200);
  }

  // 3. 3D Viewport Interaction (orbit camera around drone formation)
  console.log('[Recorder] Step 3: Interacting with 3D Canvas (orbiting camera)...');
  await smoothMoveTo(480, 360, 500);
  await page.evaluate(() => window.__clickRipple?.()).catch(() => {});
  await page.mouse.down();
  for (let ang = 0; ang <= Math.PI * 0.9; ang += 0.12) {
    const dragX = 480 + Math.sin(ang) * 150;
    const dragY = 360 - (1 - Math.cos(ang)) * 50;
    await page.mouse.move(dragX, dragY);
    await page.evaluate(({ dragX, dragY }) => window.__updateCursor?.(dragX, dragY), { dragX, dragY }).catch(() => {});
    await page.waitForTimeout(30);
  }
  await page.mouse.up();
  await page.waitForTimeout(400);

  // 4. View Modes (Top-Down, Audience, 3D Stage)
  console.log('[Recorder] Step 4: Switching 3D Camera Modes...');
  const topDownTab = page.locator('button.view-tab', { hasText: 'TOP-DOWN' });
  if (await topDownTab.isVisible().catch(() => false)) {
    await clickLocator(topDownTab);
    await page.waitForTimeout(800);
  }

  const audienceTab = page.locator('button.view-tab', { hasText: 'AUDIENCE' });
  if (await audienceTab.isVisible().catch(() => false)) {
    await clickLocator(audienceTab);
    await page.waitForTimeout(800);
  }

  const stage3dTab = page.locator('button.view-tab', { hasText: '3D STAGE' });
  if (await stage3dTab.isVisible().catch(() => false)) {
    await clickLocator(stage3dTab);
    await page.waitForTimeout(600);
  }

  // 5. Playback timeline control
  console.log('[Recorder] Step 5: Scrubbing timeline and previewing playback...');
  const playBtn = page.locator('button[title="Play"], button[aria-label="Play"]').first();
  if (await playBtn.isVisible().catch(() => false)) {
    await clickLocator(playBtn);
    await page.waitForTimeout(2500); // let playhead advance
    const pauseBtn = page.locator('button[title="Pause"], button[aria-label="Pause"]').first();
    if (await pauseBtn.isVisible().catch(() => false)) {
      await clickLocator(pauseBtn);
    }
  }

  // 6. Control Console tabs walkthrough
  console.log('[Recorder] Step 6: Touring Control Console panels...');
  const lightTab = page.locator('button.drawer-tab-btn', { hasText: 'LIGHT' });
  if (await lightTab.isVisible().catch(() => false)) {
    await clickLocator(lightTab);
    await page.waitForTimeout(800);
  }

  const audioTab = page.locator('button.drawer-tab-btn', { hasText: 'AUDIO' });
  if (await audioTab.isVisible().catch(() => false)) {
    await clickLocator(audioTab);
    await page.waitForTimeout(800);
  }

  const aiTab = page.locator('button.drawer-tab-btn', { hasText: 'AI' });
  if (await aiTab.isVisible().catch(() => false)) {
    await clickLocator(aiTab);
    await page.waitForTimeout(800);
  }

  const safetyTab = page.locator('button.drawer-tab-btn', { hasText: 'SAFETY' });
  if (await safetyTab.isVisible().catch(() => false)) {
    await clickLocator(safetyTab);
    await page.waitForTimeout(1200); // show green validation
  }

  const transmitTab = page.locator('button.drawer-tab-btn', { hasText: 'TRANSMIT' });
  if (await transmitTab.isVisible().catch(() => false)) {
    await clickLocator(transmitTab);
    await page.waitForTimeout(900);
  }

  // 7. Sidebar Navigation walkthrough
  console.log('[Recorder] Step 7: Touring Sidebar pages (Fleet & Vault)...');
  const fleetNav = page.locator('button.nav-btn', { hasText: 'Swarm Fleet' });
  if (await fleetNav.isVisible().catch(() => false)) {
    await clickLocator(fleetNav);
    await page.waitForTimeout(1200);
  }

  const vaultNav = page.locator('button.nav-btn', { hasText: 'Project Vault' });
  if (await vaultNav.isVisible().catch(() => false)) {
    await clickLocator(vaultNav);
    await page.waitForTimeout(1200);
  }

  const choreoNav = page.locator('button.nav-btn', { hasText: 'Sequencer' });
  if (await choreoNav.isVisible().catch(() => false)) {
    await clickLocator(choreoNav);
    await page.waitForTimeout(1000);
  }

  // Final smooth return to center
  await smoothMoveTo(720, 450, 500);
  await page.waitForTimeout(1500);

  // Close context to finish video recording
  console.log('[Recorder] Finalizing recording and saving video...');
  const videoObj = page.video();
  await context.close();
  await browser.close();
  server.close();

  if (videoObj) {
    const rawPath = await videoObj.path();
    const finalMp4 = path.join(recordingsDir, 'taramandal-studio-walkthrough.mp4');
    console.log(`[Recorder] Converting ${rawPath} -> ${finalMp4}`);
    try {
      execSync(`ffmpeg -y -i "${rawPath}" -c:v libx264 -pix_fmt yuv420p -movflags +faststart -r 30 "${finalMp4}"`, {
        stdio: 'inherit',
      });
      // Remove temp raw webm
      if (fs.existsSync(rawPath)) fs.unlinkSync(rawPath);
      console.log(`[Recorder] SUCCESS! Video saved at: ${finalMp4}`);
    } catch (err) {
      console.error('[Recorder] FFmpeg conversion warning:', err.message);
    }
  }
}

main().catch((err) => {
  console.error('[Recorder] Error:', err);
  process.exit(1);
});
