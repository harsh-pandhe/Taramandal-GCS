import { defineConfig, devices } from '@playwright/test';

// Visual baseline for the Studio UI (UI standard, step 0): one screenshot per
// page, console tab and modal, so each later step of the UI migration can show
// it changed only what it meant to.
//
// Baselines are kept per platform, and the Linux ones CI checks are only
// reproducible inside the pinned Playwright image the CI job runs in. Regenerate
// them with `npm run test:e2e:docker:update` from any OS, never from a native
// Linux run, whose fonts and GL stack won't match CI's.
export default defineConfig({
  testDir: './e2e',
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  timeout: 60_000,

  snapshotPathTemplate: '{testDir}/{testFileDir}/{testFileName}-snapshots/{arg}{-projectName}{-platform}{ext}',

  use: {
    baseURL: 'http://localhost:5184',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },

  expect: {
    toHaveScreenshot: {
      // Flat UI rendered by the same pinned image every run, so this is tight
      // on purpose: a relabelled button or a 1 px shift must fail. A ratio
      // (0.2% of a 1440x900 frame is ~2,600 px) was measured to let the whole
      // 3D scene appear or vanish unnoticed.
      maxDiffPixels: 20,
      threshold: 0.1,
      animations: 'disabled',
      caret: 'hide',
    },
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
  ],

  webServer: {
    command: 'npm run dev -- --port 5184 --strictPort',
    // localhost, not 127.0.0.1: Vite's dev server can bind ::1 only.
    url: 'http://localhost:5184',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
