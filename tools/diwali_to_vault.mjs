#!/usr/bin/env node
// Puts a built Diwali show into Studio's Project Vault, so the demo opens it in the Studio
// UI (Full-Screen Preview) instead of rebuilding it by hand.
//
//   node tools/diwali_to_vault.mjs --storyboard examples/diwali/storyboard_35.json \
//        --show dist/diwali/diwali_35.json [--api http://127.0.0.1:8001] [--name "Diwali 35"]
//
// The scenes come from the storyboard (the same makeScene call tools/diwali_build.mjs uses),
// the fleet from the Studio's own defaultFleet, and the built trajectory from the certified
// export, so what the preview plays is the file that was certified. Read-only use of the
// Studio engine; nothing under packages/ changes. Needs the Studio backend running
// (`./tara up`). Prints the project id.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const engine = (f) => import(path.join(root, 'packages/tara-studio-web/src/engine', f));
const { makeScene } = await engine('sequencer.js');
const { defaultFleet } = await engine('fleetCommands.js');

const arg = (name, dflt) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : dflt;
};
const storyboardPath = path.resolve(arg('storyboard'));
const showPath = path.resolve(arg('show'));
const api = arg('api', 'http://127.0.0.1:8001');
const sb = JSON.parse(fs.readFileSync(storyboardPath, 'utf8'));
const show = JSON.parse(fs.readFileSync(showPath, 'utf8'));
const name = arg('name', sb.name);

const scenes = sb.scenes.map(({ svgFile, ...s }) => makeScene({
  ...s,
  ...(svgFile ? { shapePayload: fs.readFileSync(path.join(path.dirname(storyboardPath), svgFile), 'utf8') } : {}),
}));

const body = {
  name,
  document: { scenes, dronesList: defaultFleet(sb.n), rate: sb.rate_hz ?? 10, homeLat: 12.9716, homeLon: 77.5946 },
  show,
  validation: null,
};
const res = await fetch(`${api}/api/projects`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});
const out = await res.json();
if (res.status !== 201) {
  console.error(`vault refused the project: HTTP ${res.status} ${JSON.stringify(out).slice(0, 400)}`);
  process.exit(1);
}
if (out.warnings?.length) console.error(`warnings: ${JSON.stringify(out.warnings)}`);
console.log(`${out.project.id}\t${out.project.name}\thas_show=${out.project.has_show}`);
