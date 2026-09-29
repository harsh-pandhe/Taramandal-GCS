# Local CI

Run the exact same quality gates `.github/workflows/ci.yml` runs, entirely on
your own machine, on demand — no GitHub-hosted runner minutes, no
always-on server. `./scripts/ci` and the CI workflow call the same
`scripts/ci-gates/*.sh` scripts, so there is one definition of what each
gate means, not two.

## 1. Prerequisites

- **Python 3.13** — pinned in [`.python-version`](../.python-version).
- **Node 20** — pinned in [`.nvmrc`](../.nvmrc).
- **Docker** — only needed for the `test-e2e` gate (visual regression, via
  the existing `packages/tara-web/e2e/run-in-docker.mjs` harness). The other
  four gates don't need it.
- **[`gh`](https://cli.github.com/)** — only needed for `--report-status`.

If you use `pyenv`/`nvm`, both tools read `.python-version`/`.nvmrc`
automatically. Nothing else in the repo manages versions, so there's no
second system to keep in sync.

## 2. Initial dependency install

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

Frontend dependencies (`npm ci` in `packages/tara-web` and
`packages/tara-studio-web`) are installed automatically by the gate scripts
that need them — no separate step.

## 3. Run everything

```bash
./scripts/ci
```

Runs all 5 gates in the same order as the GitHub workflow, prints a
`PASS`/`FAIL` summary line per gate, and exits non-zero if any gate failed.
Each gate's full output is also saved to `/tmp/taramandal-ci-<gate>.log`.

## 4. Run one gate

```bash
./scripts/ci --job lint-and-typecheck
./scripts/ci --job python-tests
./scripts/ci --job frontend-tests
./scripts/ci --job scale-regression
./scripts/ci --job test-e2e
./scripts/ci --list          # print the gate names
```

## 5. Optional: report a GitHub commit status

```bash
./scripts/ci --report-status
```

Runs all 5 gates, then — **only if they actually ran and only reflecting
their real result** — posts a `taramandal/local-ci` commit status for the
current `HEAD` SHA via `gh api`. The status is never posted speculatively
and never hardcoded to success; it's whatever `./scripts/ci`'s own exit
code was.

This needs `gh` authenticated with a token that has, at minimum,
**"Commit statuses: Read and write"** for this repository — a
fine-grained personal access token scoped to this one repo, not a classic
broad `repo`-scope token. Create one at
`github.com/settings/personal-access-tokens/new`, scope it to
`harsh-pandhe/Taramandal-GCS` only, grant only that one repository
permission, then `gh auth login` (or `gh auth refresh`) with it. The token
is never read from, or written to, any file in this repository, and this
script never prints it.

`taramandal/local-ci` is a distinct status context from the GitHub-hosted
`Taramandal Monorepo CI` checks — a green `taramandal/local-ci` means "a
team member ran the real suite on their own machine and it passed," which
is trusted-team distributed CI, not an independent third-party build. It
doesn't replace review or merge authority.

## 6. Verifying the workflow YAML itself (occasional, not the daily path)

`./scripts/ci` runs the gate scripts directly — it's the fast day-to-day
path. To check that `.github/workflows/ci.yml` *itself* still parses and
wires those scripts up correctly (worth doing after editing the workflow
file, not before every PR), use [`act`](https://github.com/nektos/act):

```bash
act -l                                    # list jobs/triggers act sees
act pull_request -j lint-and-typecheck    # run one job through a real
                                           # simulated GitHub runner
```

On first run, `act` asks which runner-image size to use — pick **Medium**
(~500MB; this repo's jobs only use standard, well-supported actions like
`actions/checkout` and `actions/setup-python`, so Medium is sufficient).
Avoid the **Large** image (17GB download, 75GB storage) — nothing here
needs it. To skip the prompt every time, answer once and let `act` write
`~/.config/act/actrc`, or pass `-P ubuntu-latest=catthehacker/ubuntu:act-latest`
explicitly.

`test-e2e`'s job-level `container:` directive (the pinned Playwright image)
works the same way under `act` as it does on GitHub-hosted runners — no
special handling needed.

## 7. Common failures

| Symptom | Cause | Fix |
|---|---|---|
| `ruff`/`mypy`: command not found | `requirements-dev.txt` not installed | `pip install -r requirements.txt -r requirements-dev.txt` |
| `frontend-tests` or `lint-and-typecheck` fails immediately on `npm ci` | Node version mismatch, or no network | Check `node --version` matches `.nvmrc` (20); `npm ci` needs the registry reachable |
| `test-e2e` fails with "Could not run docker" | Docker not installed or not running | Start Docker, or skip this gate locally and let CI catch visual regressions (document in your PR if you do) |
| `test-e2e` snapshot diffs that don't reproduce on GitHub-hosted CI | Running Playwright natively instead of via Docker | Use `./scripts/ci --job test-e2e` (routes through the pinned image), not a bare `npx playwright test` |
| `scale-regression` times out or is very slow | Expected — it flies a real 100-drone simulated show; give it the same ~10 minutes CI's `timeout-minutes` allows | — |
| `--report-status` fails with a 403/404 | Token missing "Commit statuses" permission, or scoped to the wrong repo | Re-create the fine-grained PAT per §5 |
