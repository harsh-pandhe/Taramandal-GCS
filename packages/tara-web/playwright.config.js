import { defineConfig, devices } from '@playwright/test';

// Visual-regression + performance harness for the night-sky viewport (issue #23).
// Runs against the Vite dev server so the harness page can import the real
// component sources directly, with no separate build step to keep in sync.
export default defineConfig({
  testDir: './e2e',
  // WebGL frame-timing measurements are not meaningful when several browser
  // instances are competing for the same GPU, so these run serially.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  timeout: 120_000,

  use: {
    baseURL: 'http://localhost:5183',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },

  expect: {
    toHaveScreenshot: {
      // Bloom is a multi-pass float-buffer effect: exact pixel values differ
      // between GPU vendors and driver versions even at identical geometry.
      // The threshold is set to absorb that while still failing on the thing
      // these tests actually guard — a drone rendering in the wrong colour
      // band, which changes a large, saturated region of the frame.
      maxDiffPixelRatio: 0.02,
      threshold: 0.25,
    },
  },

  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1280, height: 720 },
        launchOptions: {
          // Headless Chromium defaults to SwiftShader (software GL), which is
          // far too slow to say anything meaningful about a 60 FPS budget.
          // These flags select the real GPU where one is available.
          args: [
            '--use-gl=angle',
            '--use-angle=default',
            '--enable-gpu',
            '--ignore-gpu-blocklist',
            '--enable-unsafe-swiftshader',
            // Chromium throttles requestAnimationFrame and timers hard in a
            // hidden/backgrounded page — down towards 1Hz. Both the FPS
            // measurement and the soak's telemetry feed depend on running at
            // real speed, so a throttled run doesn't just measure the wrong
            // number, it stretches a 15-minute soak past its own timeout.
            '--disable-background-timer-throttling',
            '--disable-backgrounding-occluded-windows',
            '--disable-renderer-backgrounding',
            '--disable-features=CalculateNativeWinOcclusion',
          ],
        },
      },
    },
  ],

  webServer: {
    command: 'npm run dev -- --port 5183 --strictPort',
    // localhost, not 127.0.0.1: Vite's dev server binds ::1 by default here,
    // so the IPv4 literal fails the readiness probe even though the server is up.
    url: 'http://localhost:5183/e2e/harness/nightsky.html',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
