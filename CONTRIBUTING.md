# Contributing

Thanks for working on Taramandal. Read this before you start — it's short.

## How work is assigned

Everything is issue-driven. Pick an issue assigned to you (or ask to be
assigned one) from the [Issues tab](../../issues), filtered by your track
label:

- `track:gcs` — `packages/tara-gcs`, `packages/tara-sim`, `packages/tara-web`,
  `apps/tara`, `sim/px4`
- `track:studio` — `packages/tara-studio`, `packages/tara-studio-web`

**Work one track at a time.** If you're on a `track:gcs` issue, your PR
should only touch `tara-gcs`/`tara-sim`/`tara-web`/`apps/tara` files (plus
`packages/tara-core` if the issue explicitly says so — it's shared, so
changes there need to be called out, not incidental). Same the other
direction for `track:studio`. If a fix genuinely needs to cross both, say so
in the issue first rather than bundling it into your PR — it'll usually mean
the issue needs to be split or `tara-core` needs to change instead.

## Branching and PRs

There is no bot enforcing this (small private repo, no branch protection
available on our current plan) — treat it as a hard rule anyway:

1. **Never push directly to `main`.** Branch from `main`, always.
2. Branch name: `gcs/<issue-number>-short-slug` or
   `studio/<issue-number>-short-slug`, e.g. `gcs/23-fix-geofence-radius`.
3. Open a PR back to `main`, link the issue (`Closes #23`), and fill in the
   PR template.
4. **Don't merge your own PR.** A maintainer reviews and merges. If you have
   write access and are tempted to merge — don't; that's the one rule that
   actually matters here.

## Before you open a PR

- `pytest -m "not slow"` passes (Python changes). The full suite including
  the 100-drone test (`pytest`, no filter) is slow (~90s) — run it if your
  change touches `tara-sim`, `tara-gcs`, or fleet-scale behavior.
- For frontend changes: `npm run build && npm run lint` in whichever package
  you touched, clean.
- If you changed behavior an end user would notice, say what you tested and
  how in the PR description — a screenshot or a short description of a live
  run is worth more than a paragraph of explanation.
- Stay inside your track (see above). A PR that drifts outside its issue's
  scope will just get asked to split.

## Code style

Match what's already there. No drive-by reformatting, no renaming things
you're not touching, no "while I was in there" refactors — one issue, one PR,
one concern. If you spot something else worth fixing, open a new issue for it
instead of folding it into your current PR.

## Questions

If an issue is underspecified or you think the requested approach is wrong,
say so in the issue thread before writing code. Better to spend five minutes
there than to write a PR that solves the wrong problem.
