import { expect, test } from '@playwright/test';
import { signIn } from './session.js';

// Issue #98: EMERGENCY RTL and EMER LAND trigger real fleet-wide actions, so a
// single click must not fire them. The first click arms, a second click
// confirms. These run the real dashboard (App.jsx) against a stubbed backend
// and count the requests that actually reach it.

const CONFIRM_WINDOW_MS = 5000;
const CONFIRM_GUARD_MS = 350;

async function openDashboard(page) {
  const calls = { rtl: 0, land: 0, estop: 0 };
  await page.route('**/api/rtl', (route) => {
    calls.rtl += 1;
    return route.fulfill({ json: { status: 'success', message: 'Emergency RTL commanded.' } });
  });
  await page.route('**/api/land', (route) => {
    calls.land += 1;
    return route.fulfill({ json: { status: 'success', message: 'Emergency landing commanded.' } });
  });
  await page.route('**/api/estop', (route) => {
    calls.estop += 1;
    return route.fulfill({ json: { status: 'success', message: 'Motors killed.' } });
  });
  // The sim status poll would otherwise hit a GCS that isn't running.
  await page.route('**/api/sim/status', (route) => route.fulfill({ json: {} }));
  await signIn(page);
  await page.goto('/');
  return calls;
}

const rtl = (page) => page.getByRole('button', { name: /EMERGENCY RTL|CONFIRM RTL/ });
const land = (page) => page.getByRole('button', { name: /EMER LAND|CONFIRM LAND/ });
const estop = (page) => page.getByRole('button', { name: /E-STOP \(KILL\)|CONFIRM KILL/ });

// Long enough for any request a click would have sent to have been routed.
const settle = (page) => page.waitForTimeout(400);

for (const [name, button, path] of [['RTL', rtl, 'rtl'], ['Land', land, 'land'], ['E-STOP', estop, 'estop']]) {
  test(`a single click on ${name} does not fire it; a confirming click does`, async ({ page }) => {
    const calls = await openDashboard(page);

    await button(page).click();
    await settle(page);
    expect(calls[path]).toBe(0);
    await expect(button(page)).toHaveAttribute('data-armed', 'true');
    await expect(button(page)).toContainText('CONFIRM');

    await button(page).click();
    await expect.poll(() => calls[path]).toBe(1);
    await expect(button(page)).toHaveAttribute('data-armed', 'false');
  });
}

test('an armed button falls back to idle if the confirm never comes', async ({ page }) => {
  const calls = await openDashboard(page);

  await rtl(page).click();
  await expect(rtl(page)).toHaveAttribute('data-armed', 'true');
  await expect(rtl(page)).toHaveAttribute('data-armed', 'false', { timeout: CONFIRM_WINDOW_MS + 2000 });

  // The next click arms again rather than firing a stale confirmation.
  await rtl(page).click();
  await settle(page);
  expect(calls.rtl).toBe(0);
});

test('Escape cancels an armed action', async ({ page }) => {
  const calls = await openDashboard(page);

  await land(page).click();
  await expect(land(page)).toHaveAttribute('data-armed', 'true');
  await page.keyboard.press('Escape');
  await expect(land(page)).toHaveAttribute('data-armed', 'false');

  await land(page).click();
  await settle(page);
  expect(calls.land).toBe(0);
});

test('arming one emergency action disarms the other', async ({ page }) => {
  const calls = await openDashboard(page);

  await land(page).click();
  await rtl(page).click();
  await expect(land(page)).toHaveAttribute('data-armed', 'false');
  await expect(rtl(page)).toHaveAttribute('data-armed', 'true');

  // Clicking LAND now only re-arms LAND; RTL's pending confirm can't be
  // redirected into a landing.
  await land(page).click();
  await settle(page);

  // Arming E-STOP takes over from LAND too: only ever one armed emergency.
  await estop(page).click();
  await expect(land(page)).toHaveAttribute('data-armed', 'false');
  await expect(estop(page)).toHaveAttribute('data-armed', 'true');
  await settle(page);
  expect(calls).toEqual({ rtl: 0, land: 0, estop: 0 });
});

test('a reflexive double-click does not count as a confirmation', async ({ page }) => {
  const calls = await openDashboard(page);

  await rtl(page).dblclick();
  await settle(page);
  expect(calls.rtl).toBe(0);
  await expect(rtl(page)).toHaveAttribute('data-armed', 'true');

  // Past the guard, the very next click confirms, so a genuine emergency pays
  // one extra click and nothing more.
  await page.waitForTimeout(CONFIRM_GUARD_MS);
  await rtl(page).click();
  // Default poll timeout (5s) once timed out here on a severely loaded CI
  // runner (issue #149): the click itself was correct (confirmed by the
  // dblclick assertion above passing in the same run), but the route handler
  // hadn't fired yet when the poll gave up — the whole suite ran 5-10x its
  // normal duration that run (one spec file alone took 8.1m). Give this one
  // assertion slack a loaded runner needs without weakening what it checks.
  await expect.poll(() => calls.rtl, { timeout: 15_000 }).toBe(1);
});
