#!/usr/bin/env node
// Recording 2 of docs/recording-checklist.md: the Studio full-screen preview, tier `plan` (playback; nothing flew).
//
// Unlike record_studio_walkthrough.mjs (which answers validate, energy, signing and chain-assignment with canned responses),
// this drives the REAL Studio against its REAL backend: the project is loaded from the Project Vault, Generate Sequence
// builds through the backend, the badges are whatever the validator says, and the preview plays the built trajectory.
// It records the screen with ffmpeg (x11grab), so it needs a display, a real GPU for a faithful frame rate, ffmpeg, and
// Studio running (backend :8001, web :5174) with the project already in the vault (tools/diwali_to_vault.mjs).
//
//   DISPLAY=:1 FFMPEG=/path/to/ffmpeg node tools/record/record_studio_fullscreen.mjs \
//       --out /path/to/clip.mp4 --label "plan playback, nothing flew | 35 drones | main <sha7>" [--project "Diwali 2026 (35 drones)"]
//
// The capture region is 1440x926 at (0,60): the 1440x900 page plus the window title bar. The caption bar sits at the bottom of the page and stays through the full-screen
// toggle. No audio, no pointer. The script fails (non-zero) if a badge is not what a certified show must show.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '../../packages/tara-studio-web/node_modules/@playwright/test/index.mjs';

const arg = (name, dflt) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : dflt;
};
const OUT = arg('out');
const LABEL = arg('label', 'plan playback, nothing flew');
const PROJECT = arg('project', 'Diwali 2026 (35 drones)');
const URL = arg('url', 'http://localhost:5174/');
const FFMPEG = process.env.FFMPEG || 'ffmpeg';
const DISPLAY = process.env.DISPLAY || ':0';
if (!OUT) { console.error('usage: --out <clip.mp4> [--label ...] [--project ...]'); process.exit(2); }
const W = 1440, H = 900, X = 0, Y = 60;
const CAP_H = 926;   // the window manager's title bar (~26 px) sits above the 900 px page, so capture H + title bar to include the caption bar
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'studio-rec-'));
// No password manager: its "Save password?" bubble would sit over the clip after the sign-in.
fs.mkdirSync(path.join(profile, 'Default'), { recursive: true });
fs.writeFileSync(path.join(profile, 'Default', 'Preferences'), JSON.stringify({
  credentials_enable_service: false, profile: { password_manager_enabled: false }, autofill: { profile_enabled: false, credit_card_enabled: false },
}));
const context = await chromium.launchPersistentContext(profile, {
  headless: false,
  viewport: null,
  args: [`--app=${URL}`, `--window-position=${X},${Y}`, `--window-size=${W},${H}`, '--no-first-run', '--disable-infobars'],
});
const page = context.pages()[0] || (await context.newPage());
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
const fail = async (msg) => { console.error('FAIL:', msg); await context.close(); process.exit(1); };
const badges = async () => (await page.locator('[class*=badge]').allInnerTexts()).map((s) => s.replace(/\s+/g, ' ').trim());

await page.waitForLoadState('domcontentloaded');
// The caption: a fixed bar that survives the sign-in reload and the full-screen toggle. addInitScript runs on every page load, and the
// observer puts the bar back if the app's own rendering ever removes it.
await context.addInitScript((label) => {
  const make = () => {
    if (document.getElementById('rec-caption') || !document.body) return;
    const bar = document.createElement('div');
    bar.id = 'rec-caption';
    bar.textContent = label;
    Object.assign(bar.style, { position: 'fixed', left: '0', right: '0', bottom: '0', zIndex: '2147483647', padding: '6px 12px',
      font: '600 13px ui-monospace, monospace', background: '#111827', color: '#fde68a', borderTop: '1px solid #f59e0b', pointerEvents: 'none' });
    document.body.appendChild(bar);
  };
  document.addEventListener('DOMContentLoaded', make);
  new MutationObserver(make).observe(document, { childList: true, subtree: true });
  make();
}, LABEL);
await page.reload();
await page.waitForLoadState('domcontentloaded');

// 0. sign in and open the Project Vault BEFORE recording: the clip starts at the vault page
await page.getByPlaceholder(/authorization key/i).fill('TARAMANDAL_2026');
await page.getByRole('button', { name: 'AUTHORIZE CONNECTION' }).click();
await sleep(3000);
await page.getByRole('button', { name: /Project Vault/i }).first().click();
await sleep(2000);

// 1. record
fs.mkdirSync(path.dirname(path.resolve(OUT)), { recursive: true });
const ff = spawn(FFMPEG, ['-y', '-f', 'x11grab', '-draw_mouse', '0', '-framerate', '30', '-video_size', `${W}x${CAP_H}`, '-i', `${DISPLAY}.0+${X},${Y}`,
  '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p', '-an', path.resolve(OUT)], { stdio: ['pipe', 'ignore', 'ignore'] });
log('recording started');
await sleep(2500);

// 2. open the project from the Project Vault
await page.getByRole('button', { name: 'Load' }).first().click();
await sleep(2000);
await page.getByRole('button', { name: 'Sequencer' }).first().click();
await sleep(2500);
const before = await badges();
log('badges after opening the project:', JSON.stringify(before));
if (!before.some((b) => /Refused/.test(b))) await fail(`expected a Refused badge before the build, got ${JSON.stringify(before)}`);
await sleep(3000);

// 3. Generate Sequence: Built and Safety passed
await page.getByRole('button', { name: /Generate Sequence/i }).click();
await page.getByText(/^Built · /).waitFor({ timeout: 180000 });
await page.getByText('Safety passed · exports enabled').waitFor({ timeout: 120000 });
const built = await badges();
log('badges after the build:', JSON.stringify(built));
if (built.some((b) => /exports locked/.test(b))) await fail(`not a certified show: ${JSON.stringify(built)}`);
await sleep(5000);

// 4. full-screen preview, play at 1x, then 4x to the end, Esc
await page.getByRole('button', { name: /FULL-SCREEN PREVIEW/ }).click();
await sleep(2500);
await page.getByRole('button', { name: 'Play' }).first().click();
log('playing at 1x');
await sleep(45000);
await page.getByRole('button', { name: '4x' }).click();
log('playing at 4x');
// Poll the position and pause at the landing: left to run it would loop back to 0.
const readPos = async () => (await page.locator('text=/\\d+\\.\\d+s \\/ \\d+\\.\\d+s/').first().innerText().catch(() => '')).trim();
let pos = '';
for (let i = 0; i < 160; i++) {
  pos = await readPos();
  const m = pos.match(/^(\d+\.\d+)s \/ (\d+\.\d+)s/);
  if (m && parseFloat(m[1]) >= parseFloat(m[2]) - 3) break;
  await sleep(500);
}
await page.getByRole('button', { name: 'Pause' }).first().click();
await sleep(4000);
pos = await readPos();
log('playback position:', pos);
await page.keyboard.press('Escape');
await sleep(3500);
const back = await page.getByRole('button', { name: /FULL-SCREEN PREVIEW/ }).count();
log('full-screen exited with Esc:', back === 1);
if (back !== 1) await fail('Esc did not leave full-screen');
await sleep(2000);

// 5. stop recording
ff.stdin.write('q');
await new Promise((r) => ff.on('close', r));
await context.close();
fs.rmSync(profile, { recursive: true, force: true });
log('recording written:', path.resolve(OUT), `${(fs.statSync(path.resolve(OUT)).size / 1e6).toFixed(1)} MB`, '| page errors:', JSON.stringify(errors));
