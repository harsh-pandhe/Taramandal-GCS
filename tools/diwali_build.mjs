#!/usr/bin/env node
// Builds a Diwali storyboard with Studio's own engine and certifies it through
// the real backend /api/export. Read-only use of the engine: nothing under
// packages/tara-studio-web/src is modified.
//
//   node tools/diwali_build.mjs --storyboard examples/diwali/storyboard_100.json \
//        [--n 100] [--rate 10] [--api http://127.0.0.1:8001] [--out dist/diwali]
//
// Needs the Studio backend running (see tools/diwali_run_all.sh). Exits 0 only
// when the flight (non-preview) export is accepted and every check holds.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const engine = (f) => import(path.join(root, 'packages/tara-studio-web/src/engine', f));
const { buildSequencedShow, computeAssignmentChainLocal, computeShowDuration, makeScene } = await engine('sequencer.js');

const MIN_SEPARATION_M = 2.0;
const CEILING_M = 120.0;

const arg = (name, dflt) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : dflt;
};
const storyboardPath = path.resolve(arg('storyboard'));
const api = arg('api', 'http://127.0.0.1:8001');
const outDir = path.resolve(arg('out', path.join(root, "dist", "diwali")));
const sb = JSON.parse(fs.readFileSync(storyboardPath, 'utf8'));
const n = Number(arg('n', sb.n));
const rate = Number(arg('rate', sb.rate_hz ?? 10));
// Output files are named diwali_<tag>.*; the tag defaults to the fleet size.
const tag = arg('tag', String(n));

const scenes = sb.scenes.map(({ svgFile, ...s }) => makeScene({
  ...s,
  ...(svgFile ? { shapePayload: fs.readFileSync(path.join(path.dirname(storyboardPath), svgFile), 'utf8') } : {}),
}));

const post = async (url, body) => {
  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  return { status: res.status, body: await res.json().catch(() => ({})) };
};

// Independent separation check on the exported waypoints, not trusting the
// validator's own number: every drone shares the same sample times, so compare
// every pair at each sample and at 4 sub-steps between samples (linear motion).
// Spread into Math.max overflows the stack at ~100 drones x thousands of samples.
const maxAltitude = (drones) => {
  let best = -Infinity;
  for (const d of drones) for (const w of d.waypoints) if (-w.z > best) best = -w.z;
  return best;
};

const minSeparation = (drones, subSteps) => {
  const T = drones[0].waypoints.length;
  let best = Infinity;
  const at = (d, k, f) => {
    const a = d.waypoints[k], b = d.waypoints[Math.min(k + 1, T - 1)];
    return [a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f, a.z + (b.z - a.z) * f];
  };
  for (let k = 0; k < T; k++) {
    for (const f of k === T - 1 || subSteps === 0 ? [0] : [0, 0.25, 0.5, 0.75]) {
      const pts = drones.map((d) => at(d, k, f));
      for (let i = 0; i < pts.length; i++) {
        for (let j = i + 1; j < pts.length; j++) {
          const d = Math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1], pts[i][2] - pts[j][2]);
          if (d < best) best = d;
        }
      }
    }
  }
  return best;
};

const result = { storyboard: sb.name, n, rate_hz: rate, scenes: scenes.map((s) => s.name) };
const fail = (msg, extra = {}) => {
  console.log(JSON.stringify({ ...result, built: false, error: msg, ...extra }, null, 2));
  process.exit(2);
};

let show, chain;
try {
  chain = computeAssignmentChainLocal(scenes, n);
  show = buildSequencedShow(scenes, n, rate, chain);
} catch (e) {
  fail(`${e.name}: ${e.message}`);
}
const choreo = { drones: show.drones };
result.built = true;
result.duration_s = show._duration;
result.nominal_duration_s = computeShowDuration(scenes);
result.waypoints_per_drone = show.drones[0].waypoints.length;
result.max_altitude_m = maxAltitude(show.drones);

const validate = await post(`${api}/api/validate`, choreo);
const rep = validate.body;
result.validator = {
  status: rep.status,
  feasibility_certified: rep.feasibility_certified,
  metrics: rep.metrics,
  violation_counts: Object.fromEntries(Object.entries(rep.violations ?? {}).map(([k, v]) => [k, v.length])),
};

result.exports = {};
for (const format of ['json', 'tara']) {
  const r = await post(`${api}/api/export`, { choreo, format, rate_hz: rate, home_lat: 0, home_lon: 0 });
  result.exports[format] = {
    http_status: r.status,
    preview_only: r.body.preview_only ?? null,
    filename: r.body.filename ?? null,
    refusal: r.status === 200 ? null : (typeof r.body.detail === 'string' ? r.body.detail : r.body.detail?.message),
  };
  if (r.status === 200) {
    fs.mkdirSync(outDir, { recursive: true });
    const base = `diwali_${tag}`;
    if (format === 'json') {
      fs.writeFileSync(path.join(outDir, `${base}.json`), r.body.content);
      result.exports[format].bytes = r.body.content.length;
    } else {
      fs.writeFileSync(path.join(outDir, `${base}.tara`), Buffer.from(r.body.content, 'base64'));
      result.exports[format].bytes = Buffer.from(r.body.content, 'base64').length;
    }
  }
}

// Min separation measured on the JSON the backend actually returned.
const exportedJson = path.join(outDir, `diwali_${tag}.json`);
if (fs.existsSync(exportedJson)) {
  const exported = JSON.parse(fs.readFileSync(exportedJson, 'utf8'));
  const drones = exported.drones ?? exported.choreo?.drones ?? choreo.drones;
  // All-pairs over every sample is O(n^2 T): at 500 drones the 4 sub-steps between
  // samples are skipped (samples only), and the result says so.
  const subSteps = drones.length > 150 ? 0 : 4;
  result.min_separation_check = subSteps ? 'samples + 4 linear sub-steps between samples' : 'samples only (n > 150)';
  result.min_separation_m = Number(minSeparation(drones, subSteps).toFixed(4));
  result.max_altitude_exported_m = maxAltitude(drones);
}

result.checks = {
  flight_export_accepted: Object.values(result.exports).every((e) => e.http_status === 200 && e.preview_only === false),
  zero_violations: Object.values(result.validator.violation_counts).every((c) => c === 0),
  separation_at_least_2m: result.min_separation_m !== undefined && result.min_separation_m >= MIN_SEPARATION_M,
  ceiling_120m: result.max_altitude_m <= CEILING_M,
};
fs.mkdirSync(outDir, { recursive: true });
fs.writeFileSync(path.join(outDir, `diwali_${tag}.result.json`), JSON.stringify(result, null, 2));
console.log(JSON.stringify(result, null, 2));
process.exit(Object.values(result.checks).every(Boolean) ? 0 : 1);
