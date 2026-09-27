// Runs the Playwright suite inside the same pinned image the CI `test-e2e` job
// uses, so the Linux snapshot baselines can be checked or regenerated from any
// OS and still match what CI renders. Arguments are passed through to
// `playwright test`, e.g.:
//
//   npm run test:e2e:docker                      # check against the baselines
//   npm run test:e2e:docker:update               # rewrite the -linux baselines
//   npm run test:e2e:docker -- -g "snapshot"     # any playwright test flags
//
// Plain Node rather than a shell one-liner in package.json so it behaves the
// same under PowerShell, cmd and bash.
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const pkgDir = resolve(dirname(fileURLToPath(import.meta.url)), '..');

// The image tag must match the installed @playwright/test exactly (the image
// ships that release's browser builds), so it is read from the lockfile rather
// than hardcoded here. Keep the image in .github/workflows/ci.yml in step.
const lock = JSON.parse(readFileSync(resolve(pkgDir, 'package-lock.json'), 'utf8'));
const version = lock.packages['node_modules/@playwright/test'].version;
const image = `mcr.microsoft.com/playwright:v${version}-jammy`;

const quote = (arg) => `'${arg.replace(/'/g, `'\\''`)}'`;
const testArgs = process.argv.slice(2).map(quote).join(' ');

// The container runs as root. On a Linux host that leaves root-owned snapshot
// and result files in the working tree, so hand them back to the caller;
// Docker Desktop (macOS/Windows) maps ownership itself and needs nothing.
const owner = typeof process.getuid === 'function' ? `${process.getuid()}:${process.getgid()}` : null;
const chown = owner ? `chown -R ${owner} e2e test-results playwright-report 2>/dev/null;` : '';

const script = `npm ci --no-audit --no-fund && { npx playwright test ${testArgs}; status=$?; ${chown} exit $status; }`;

const result = spawnSync(
  'docker',
  [
    'run', '--rm', '--init', '--ipc=host',
    '-v', `${pkgDir}:/work`,
    // Shadow the host's node_modules: it may hold another OS's native binaries.
    '-v', '/work/node_modules',
    // Persist npm's download cache across runs so each `npm ci` is offline
    // after the first, instead of refetching every package every time.
    '-v', 'tara-web-e2e-npm-cache:/root/.npm',
    '-w', '/work',
    image,
    'bash', '-c', script,
  ],
  { stdio: 'inherit' },
);

if (result.error) {
  console.error(`Could not run docker: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
