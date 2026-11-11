// Diwali 269-drone flat 2D show (13 scenes): the full light choreography (takeoff glow, dissolve, dark flight, animated reveals) and the motion blocks (diya flame, Ganesha ears/hand/lotus/swastika, Ram advance + baan, Mandir flag, Lakshmi hands/seat with the crown still, finale bursts).
//   node build_show.mjs <repo root> <formations folder> <studio api url> <out folder>     (see README.md)
import fs from 'node:fs';
import path from 'node:path';
const [root, fdir, api, outDir] = process.argv.slice(2);
const eng = (f) => import(path.join(root, 'packages/tara-studio-web/src/engine', f));
const { buildSequencedShow, computeShowDuration, buildTimelineSegments, makeScene } = await eng('sequencer.js');
const { buildFormation } = await eng('formations.js');
const { optimalAssignment } = await eng('assignment.js');
const { hoverAt } = await eng('layout.js');
const summary = JSON.parse(fs.readFileSync(path.join(fdir, 'summary.json'), 'utf8'));
const N = summary.fleet, RATE = 10, SITE = 'scale400';
const NAME = summary.scenes.map((s) => s.scene), KIND = summary.scenes.map((s) => s.kind), K = NAME.length;
const HOLD = { 'Golden Spiral': 14, 'Diya': 34, 'Happy Diwali': 28, 'Lord Ganesha': 36, 'Shri Ram': 18, 'Shri Ram advances': 2, 'The baan is loosed': 8, 'Ripple Rings': 18, 'Rangoli': 24, 'Firecrackers & Sparkles': 36, 'Ayodhya Mandir': 28, 'Lakshmi with Lotus': 32, 'Grand Finale': 34 };
const TRANS = { 'Shri Ram advances': 3, 'The baan is loosed': 9 };
const REV = { 'Golden Spiral': 6, 'Diya': 6, 'Happy Diwali': 7, 'Lord Ganesha': 6, 'Shri Ram': 6, 'Ripple Rings': 5, 'Rangoli': 6, 'Firecrackers & Sparkles': 7, 'Ayodhya Mandir': 6, 'Lakshmi with Lotus': 6, 'Grand Finale': 8 };
const forms = NAME.map((_, k) => JSON.parse(fs.readFileSync(path.join(fdir, `s${String(k + 1).padStart(2, '0')}.json`), 'utf8')));
const scenes = NAME.map((nm, k) => makeScene({ name: nm, shapeType: 'exact_points', scale: 1.0, holdDurationSeconds: HOLD[nm] ?? 14, transitionDurationSeconds: TRANS[nm] ?? 12,
  shapePayload: JSON.stringify(forms[k].points.map((q) => ({ x: q.x, y: 0, z: -q.alt }))) }));

// ---- the drone -> point assignment. Optimal everywhere, except Shri Ram -> advances -> baan: the SAME drones keep their points ------
const identity = (n) => Array.from({ length: n }, (_, i) => i);
const F = [Array.from({ length: N }, (_, i) => hoverAt(i, SITE)), ...scenes.map((s) => buildFormation(s.shapeType, s.shapePayload, N, s.scale, SITE)), Array.from({ length: N }, (_, i) => hoverAt(i, SITE))];
const LOCK = new Set([NAME.indexOf('Shri Ram advances'), NAME.indexOf('The baan is loosed')]);
const chain = [identity(N)];
for (let s = 0; s < F.length - 1; s++) {
  const sceneIdx = s;                                             // F[s + 1] is scene s (F[0] is the hover grid)
  if (LOCK.has(sceneIdx) && s >= 1) { chain.push(chain[s].slice()); continue; }
  const from = chain[s].map((k) => F[s][k]); chain.push(optimalAssignment(from, F[s + 1]));
}
const tIdx = chain.slice(1);                                      // one permutation per scene, then the return spots
const show = buildSequencedShow(scenes, N, RATE, tIdx, 'standard', SITE);
const segs = buildTimelineSegments(scenes, N, RATE, tIdx, 'standard', SITE);
const sceneIndexById = new Map(scenes.map((s, k) => [s.id, k]));
const S = segs.map((g) => ({ kind: g.kind, label: g.label, k: g.sceneId ? sceneIndexById.get(g.sceneId) : null, t0: g.start, t1: g.end }));

// ---- the diya's flame MOVES: every flame drone sways along a straight line (x), growing with height; the base stays put --------------------------
// One scalar sway function per drone (time-shifted by height so the motion travels up the flame like a wave), so each drone's path is a straight
// line and the cornering limit is never in play. Amplitudes are chosen so the sway's own jerk stays a few m/s^3 under the 20 m/s^3 ceiling.
const iDiya = NAME.indexOf('Diya'); const hdS = S.find((g) => g.kind === 'hold' && g.k === iDiya);
const FLAME_BASE = 37.0, FLAME_H = 50.0, FLAME_CX = 0.0, HL = hdS.t1 - hdS.t0;
const SW = { A1: 3.0, T1: 8.0, A2: 0.5, T2: 4.6, B: 0.020, TB: 5.0 };   // slow, wide sway: acceleration stays within the show's 3 m/s^2 PX4-safe cap
const smoothstep01 = (v) => { v = Math.max(0, Math.min(1, v)); return v * v * (3 - 2 * v); };
const envFlame = (l) => smoothstep01((l - 1.2) / 4.0) * (1 - smoothstep01((l - (HL - 5.2)) / 4.0));
let flameMoved = 0;
for (let i = 0; i < N; i++) {
  const k = tIdx[iDiya][i]; if (k >= forms[iDiya].meta.n_fig) continue;
  const p = forms[iDiya].points[k]; if (!['flame_tip', 'flame_out', 'flame_in', 'core'].includes(p.role)) continue;
  const h = Math.max(0, Math.min(1, (p.alt - FLAME_BASE) / FLAME_H)), u = Math.pow(h, 1.4), lag = 0.9 * h, ph = (i % 7) * 0.04;
  let any = false;
  for (const w of show.drones[i].waypoints) {
    const l = w.time - hdS.t0; if (l <= 0 || l >= HL) continue; const e = envFlame(l); if (e <= 0) continue;
    const tt = l - lag;
    const s = SW.A1 * Math.sin(2 * Math.PI * tt / SW.T1) + SW.A2 * Math.sin(2 * Math.PI * tt / SW.T2 + 1.3);
    const b = SW.B * Math.sin(2 * Math.PI * (l / SW.TB) + ph) * u;
    w.x = Math.round((w.x + e * (u * s + (p.x - FLAME_CX) * b)) * 1000) / 1000; any = true;
  }
  if (any) flameMoved++;
}
console.log(`flame motion: ${flameMoved} flame drones sway during the Diya hold (${HL.toFixed(0)} s), base fixed, tip amplitude up to ${(SW.A1 + SW.A2).toFixed(2)} m`);

// ---- the starbursts PULSE: each burst drone moves along its own radial line, out and back, in step with the colour wavefront ----------------------------
const iHap = NAME.indexOf('Happy Diwali'); const hdH = S.find((g) => g.kind === 'hold' && g.k === iHap), HLH = hdH.t1 - hdH.t0;
const BURST_A = 2.4, BURST_T = 7.0, BURST_T0 = 9.4;
const envBurst = (l) => smoothstep01((l - BURST_T0 + 0.6) / 2.0) * (1 - smoothstep01((l - (HLH - 5.5)) / 3.0));
let burstMoved = 0;
for (let i = 0; i < N; i++) {
  const kk = tIdx[iHap][i]; const p = forms[iHap].points[kk]; if (p.role !== 'burst') continue;
  const cs = forms[iHap].meta.bursts, c0 = cs.reduce((m, c) => (Math.hypot(p.x - c[0], p.alt - c[1]) < Math.hypot(p.x - m[0], p.alt - m[1]) ? c : m));
  const dx = p.x - c0[0], da = p.alt - c0[1], r = Math.hypot(dx, da); if (r < 1e-6) continue; const ux = dx / r, ua = da / r, phs = c0[0] > 0 ? 0.22 : 0;
  for (const w of show.drones[i].waypoints) {
    const l = w.time - hdH.t0; if (l <= 0 || l >= HLH) continue; const e = envBurst(l); if (e <= 0) continue;
    const s = e * (BURST_A / 2) * (1 - Math.cos(2 * Math.PI * ((l - BURST_T0) / BURST_T - phs))) * (r / 6.4);
    w.x = Math.round((w.x + s * ux) * 1000) / 1000; w.z = Math.round((w.z - s * ua) * 1000) / 1000;
  }
  burstMoved++;
}
console.log(`burst motion: ${burstMoved} starburst drones pulse outward up to ${(BURST_A).toFixed(1)} m during the Happy Diwali hold (${HLH.toFixed(0)} s)`);

// ---- Ganesha's ears flap and his raised hand waves left and right ------------------------------------------------------------------------------------
// Ears: each ear rotates about its hinge (tip moves up/down) and fans a little outward, in one scalar so every drone's path is a straight line.
// Hand: the fingers sway left and right about the wrist, more with height above the wrist. Everything fades in after the reveal and out before the next move.
const c01 = (v) => Math.max(0, Math.min(1, v));
const LK = { T0: 7.5, seatA: 2.4, seatUp: 0.0, seatT: 6.5, seatLag: 1.0, haloA: 2.0, haloT: 7.0, lotSway: 2.4, lotBloom: 1.4, lotT: 7.0, handA: 1.0, handT: 6.4 };
const GM = { earA: 1.8, earOut: 1.0, earT: 6.4, earLag: 0.35, handA: 2.6, handT: 7.0, lotA: 2.4, lotUp: 1.0, lotT: 6.5, lotLag: 1.0, rotT0: 8.5, rotT: 26.0 };
const iGan = NAME.indexOf('Lord Ganesha'); const hdG = S.find((g) => g.kind === 'hold' && g.k === iGan), HLG = hdG.t1 - hdG.t0;
const envG = (l) => smoothstep01((l - 5.5) / 3.5) * (1 - smoothstep01((l - (HLG - 5.0)) / 3.5));
const HING = forms[iGan].meta.hinge; let ganMoved = 0;
for (let i = 0; i < N; i++) {
  const kk = tIdx[iGan][i]; if (kk >= forms[iGan].meta.n_fig) continue; const p = forms[iGan].points[kk];
  if (p.role !== 'ear_L' && p.role !== 'ear_R' && p.role !== 'hand') continue;
  const ear = p.role !== 'hand', sg = p.role === 'ear_L' ? -1 : 1, lag = p.role === 'ear_R' ? GM.earLag : 0;
  const wgt = ear ? Math.pow(c01(((p.x - HING[p.role][0]) * sg) / 20), 1.1) : Math.pow(c01((p.alt - HING.wrist[1]) / 20), 1.2);
  for (const w of show.drones[i].waypoints) {
    const l = w.time - hdG.t0; if (l <= 0 || l >= HLG) continue; const e = envG(l); if (e <= 0) continue;
    if (ear) { const s = Math.sin(2 * Math.PI * (l - lag) / GM.earT); w.x = Math.round((w.x + e * sg * GM.earOut * wgt * s) * 1000) / 1000; w.z = Math.round((w.z - e * GM.earA * wgt * s) * 1000) / 1000; }
    else { const s = Math.sin(2 * Math.PI * l / GM.handT); w.x = Math.round((w.x + e * GM.handA * wgt * s) * 1000) / 1000; }
  }
  ganMoved++;
}
// ---- the lotus BLOOMS (petals open outward and lift a little, in a wave from the centre), and each swastika turns once, clockwise ----------------------------
const envL = (l) => smoothstep01((l - 7.0) / 3.0) * (1 - smoothstep01((l - (HLG - 5.5)) / 3.5));
let lotMoved = 0, swMoved = 0;
for (let i = 0; i < N; i++) {
  const kk = tIdx[iGan][i]; const p = forms[iGan].points[kk]; if (!p) continue;
  if (p.role === 'lotus') {
    const sg = p.x >= 0 ? 1 : -1, wh = GM.lotA * c01(Math.abs(p.x) / 41), wv = GM.lotUp * c01((p.alt - 8) / 17), lag = (Math.abs(p.x) / 41) * GM.lotLag;
    for (const w of show.drones[i].waypoints) {
      const l = w.time - hdG.t0; if (l <= 0 || l >= HLG) continue; const e = envL(l); if (e <= 0) continue;
      const s = e * (1 - Math.cos(2 * Math.PI * (l - 7.0 - lag) / GM.lotT)) / 2;
      w.x = Math.round((w.x + sg * wh * s) * 1000) / 1000; w.z = Math.round((w.z - wv * s) * 1000) / 1000;
    }
    lotMoved++;
  } else if (p.role === 'swastik') {
    const cs = forms[iGan].meta.swastiks, c0 = cs.reduce((m, c) => (Math.hypot(p.x - c[0], p.alt - c[1]) < Math.hypot(p.x - m[0], p.alt - m[1]) ? c : m)); const dx = p.x - c0[0], da = p.alt - c0[1];
    for (const w of show.drones[i].waypoints) {
      const l = w.time - hdG.t0; if (l <= GM.rotT0 || l >= HLG) continue;
      const u = Math.min(1, (l - GM.rotT0) / GM.rotT), ang = -2 * Math.PI * smoothstep01(u), ca = Math.cos(ang), sa = Math.sin(ang);
      const nx = ca * dx - sa * da, na = sa * dx + ca * da;
      w.x = Math.round((w.x + (nx - dx)) * 1000) / 1000; w.z = Math.round((w.z - (na - da)) * 1000) / 1000;
    }
    swMoved++;
  }
}
// ---- the two chakris of the Firecrackers scene each turn once, and the three Finale starbursts pulse outward -------------------------------------------------
const iFc = NAME.indexOf('Firecrackers & Sparkles'), iFin = NAME.indexOf('Grand Finale'); const hdF = S.find((g) => g.kind === 'hold' && g.k === iFc), HLF = hdF.t1 - hdF.t0;
const CH_T0 = 10.0, CH_T = 24.0; let chMoved = 0;
for (let i = 0; i < N; i++) {
  const kk = tIdx[iFc][i]; const p = forms[iFc].points[kk]; if (!p || p.role !== 'chakri') continue;
  const cs = forms[iFc].meta.chakris, c0 = cs.reduce((m, c) => (Math.hypot(p.x - c[0], p.alt - c[1]) < Math.hypot(p.x - m[0], p.alt - m[1]) ? c : m)); const dx = p.x - c0[0], da = p.alt - c0[1];
  for (const w of show.drones[i].waypoints) {
    const l = w.time - hdF.t0; if (l <= CH_T0 || l >= HLF) continue;
    const u = Math.min(1, (l - CH_T0) / CH_T), ang = -2 * Math.PI * smoothstep01(u), ca = Math.cos(ang), sa = Math.sin(ang), nx = ca * dx - sa * da, na = sa * dx + ca * da;
    w.x = Math.round((w.x + (nx - dx)) * 1000) / 1000; w.z = Math.round((w.z - (na - da)) * 1000) / 1000;
  }
  chMoved++;
}
const hdN = S.find((g) => g.kind === 'hold' && g.k === iFin), HLN = hdN.t1 - hdN.t0; let finMoved = 0;
const envFin = (l) => smoothstep01((l - 10.5) / 4.5) * (1 - smoothstep01((l - (HLN - 6.0)) / 4.5));
for (let i = 0; i < N; i++) {
  const kk = tIdx[iFin][i]; const p = forms[iFin].points[kk]; if (!p || p.role !== 'burst') continue;
  const cs = forms[iFin].meta.bursts, c0 = cs.reduce((m, c) => (Math.hypot(p.x - c[0], p.alt - c[1]) < Math.hypot(p.x - m[0], p.alt - m[1]) ? c : m)); const dx = p.x - c0[0], da = p.alt - c0[1], r = Math.hypot(dx, da); if (r < 1e-6) continue;
  const ux = dx / r, ua = da / r, phs = 0.11 * cs.indexOf(c0);
  for (const w of show.drones[i].waypoints) {
    const l = w.time - hdN.t0; if (l <= 0 || l >= HLN) continue; const e = envFin(l); if (e <= 0) continue;
    const sv = e * (2.4 / 2) * (1 - Math.cos(2 * Math.PI * ((l - 12.0) / 7.0 - phs))) * (r / 6.4);
    w.x = Math.round((w.x + sv * ux) * 1000) / 1000; w.z = Math.round((w.z - sv * ua) * 1000) / 1000;
  }
  finMoved++;
}
// ---- Shri Ram steps forward at the baan's speed; the Mandir flag flutters; Lakshmi's lotuses, halo, seat and hands move --------------------------------------------------
{
  const advSeg = S.find((g) => g.kind === 'morph' && g.label === 'Morph → Shri Ram advances'); const FAST_T = 5.4, t0 = advSeg.t0, t1 = advSeg.t1, wait = (t1 - t0) - FAST_T;
  const smoother = (u) => u * u * u * (u * (6 * u - 15) + 10); let n = 0;
  for (let i = 0; i < N; i++) {
    const wps = show.drones[i].waypoints; let a = 0, b = wps.length - 1; while (a + 1 < wps.length && wps[a + 1].time <= t0 + 1e-6) a++; while (b > 0 && wps[b - 1].time >= t1 - 1e-6) b--;
    const P0 = wps[a], P1 = wps[b]; if (Math.hypot(P1.x - P0.x, P1.y - P0.y, P1.z - P0.z) < 1e-6) continue;
    for (let k = a; k <= b; k++) { const u = Math.max(0, Math.min(1, (wps[k].time - t0 - wait) / FAST_T)), sv = smoother(u); wps[k].x = Math.round((P0.x + (P1.x - P0.x) * sv) * 1000) / 1000; wps[k].y = Math.round((P0.y + (P1.y - P0.y) * sv) * 1000) / 1000; wps[k].z = Math.round((P0.z + (P1.z - P0.z) * sv) * 1000) / 1000; }
    n++;
  }
  console.log(`ram advance retimed: ${n} drones step forward in ${FAST_T} s (peak ${(1.875 * 5 / FAST_T).toFixed(2)} m/s) after holding still for ${wait.toFixed(1)} s`);
}
const iMan = NAME.indexOf('Ayodhya Mandir'); const hdM = S.find((g) => g.kind === 'hold' && g.k === iMan), HLM = hdM.t1 - hdM.t0; let flagMoved = 0;
const envM = (l) => smoothstep01((l - 8.5) / 3.5) * (1 - smoothstep01((l - (HLM - 4.5)) / 3.5));
for (let i = 0; i < N; i++) {
  const kk = tIdx[iMan][i]; const p = forms[iMan].points[kk]; if (!p || p.role !== 'mflag') continue;
  const dist = p.x - forms[iMan].meta.flag_pole_x, amp = 1.0 * Math.pow(Math.max(0, Math.min(1, dist / 20)), 0.8);
  for (const w of show.drones[i].waypoints) {
    const l = w.time - hdM.t0; if (l <= 0 || l >= HLM) continue; const e = envM(l); if (e <= 0) continue;
    w.z = Math.round((w.z - e * amp * Math.sin(2 * Math.PI * ((l - 8.5) / 4.0 - dist / 20))) * 1000) / 1000;
  }
  flagMoved++;
}
const iLak = NAME.indexOf('Lakshmi with Lotus'); const hdK = S.find((g) => g.kind === 'hold' && g.k === iLak), HLK = hdK.t1 - hdK.t0; let lakMoved = 0;
const envK = (l) => smoothstep01((l - 7.0) / 3.5) * (1 - smoothstep01((l - (HLK - 5.5)) / 3.5));
const hlot = { '-1': [0, 0, 0], '1': [0, 0, 0] };
for (const q of forms[iLak].points.slice(0, forms[iLak].meta.n_fig)) if (q.role === 'hlotus') { const h = hlot[q.x < 0 ? '-1' : '1']; h[0] += q.x; h[1] += q.alt; h[2] += 1; }
for (const key of ['-1', '1']) { const h = hlot[key]; if (h[2]) { h[0] /= h[2]; h[1] /= h[2]; } }
for (let i = 0; i < N; i++) {
  const kk = tIdx[iLak][i]; const p = forms[iLak].points[kk]; if (!p || !p.role) continue; const wp = show.drones[i].waypoints;
  const apply = (fn) => { for (const w of wp) { const l = w.time - hdK.t0; if (l <= 0 || l >= HLK) continue; const e = envK(l); if (e <= 0) continue; const [ddx, ddz] = fn(l, e); w.x = Math.round((w.x + ddx) * 1000) / 1000; w.z = Math.round((w.z + ddz) * 1000) / 1000; } lakMoved++; };
  if (p.role === 'seat') { const sg = p.x >= 0 ? 1 : -1, wh = LK.seatA * c01(Math.abs(p.x) / 50.6), wv = LK.seatUp * c01((p.alt - 8) / 27), lag = (Math.abs(p.x) / 50.6) * LK.seatLag; apply((l, e) => { const s = e * (1 - Math.cos(2 * Math.PI * (l - LK.T0 - lag) / LK.seatT)) / 2; return [sg * wh * s, -wv * s]; }); }
  else if (p.role === 'hlotus') {
    const side = p.x < 0 ? -1 : 1, c = hlot[String(side)], dx = p.x - c[0], da = p.alt - c[1], r = Math.hypot(dx, da) || 1, wr = Math.min(1, r / 6);
    apply((l, e) => { const ph = 2 * Math.PI * (l - LK.T0) / LK.lotT, out = (1 - Math.cos(ph)) / 2, sway = side * LK.lotSway * out, bl = out * LK.lotBloom * wr; return [e * (sway + (dx / r) * bl), -e * (da / r) * bl]; });
  }
  // the halo and the crown do NOT move (moving them distorted the head); they keep only their light effects
  else if (p.role === 'lhand') { const w8 = Math.pow(c01((Math.abs(p.x) - 16) / 22), 1.2), ph0 = p.x < 0 ? Math.PI : 0; apply((l, e) => [0, -e * LK.handA * w8 * Math.sin(2 * Math.PI * (l - LK.T0) / LK.handT + ph0)]); }
}
console.log(`mandir flag: ${flagMoved} cloth drones flutter; lakshmi: ${lakMoved} drones move (hand lotuses, seat, lower hands; crown and halo stay still)`);
console.log(`chakri turn: ${chMoved} drones; finale starburst pulses: ${finMoved} drones`);
console.log(`lotus bloom: ${lotMoved} lotus drones; swastika turn: ${swMoved} drones (one clockwise turn in ${GM.rotT} s)`);
console.log(`ganesha motion: ${ganMoved} drones (2 ears + hand) move during the Ganesha hold (${HLG.toFixed(0)} s)`);

// ---- colour helpers ----------------------------------------------------------------------------------------------------------
const clamp01 = (v) => Math.max(0, Math.min(1, v));
const mix = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
const scl = (c, s) => [c[0] * s, c[1] * s, c[2] * s];
const smooth = (t) => { t = clamp01(t); return t * t * (3 - 2 * t); };
const fract = (v) => v - Math.floor(v);
const hash = (i, b) => fract(Math.sin(i * 12.9898 + b * 78.233) * 43758.5453);
const ramp = (stops, u) => { const m = stops.length - 1; const p = clamp01(u) * m; const i = Math.min(m - 1, Math.floor(p)); return mix(stops[i], stops[i + 1], p - i); };
const hsv = (h, s, v) => { const i = Math.floor(h * 6), f = h * 6 - i, p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s); return [[v, t, p], [q, v, p], [p, v, t], [p, q, v], [t, p, v], [v, p, q]][((i % 6) + 6) % 6].map((c) => c * 255); };
const WHITE = [255, 255, 255], GOLD = [255, 196, 40], WARM = [255, 214, 150], BLACK = [0, 0, 0];
const SPIRAL_PAL = [[255, 205, 60], [255, 130, 25], [240, 60, 190], [70, 150, 255], [255, 205, 60]];

// ---- per-scene lookups ---------------------------------------------------------------------------------------------------------
const pt = (k, i) => forms[k].points[tIdx[k][i]];
const lit = (k, i) => tIdx[k][i] < forms[k].meta.n_fig;
const base = (k, i) => { const p = pt(k, i); return [p.r, p.g, p.b]; };
const aMin = [], aRange = [], aMid = [];
forms.forEach((f, k) => { const a = f.points.slice(0, f.meta.n_fig).map((p) => p.alt); const lo = Math.min(...a), hi = Math.max(...a); aMin[k] = lo; aRange[k] = Math.max(1, hi - lo); aMid[k] = (lo + hi) / 2; });
const alt01 = (k, p) => clamp01((p.alt - aMin[k]) / aRange[k]);
const isArrow = (p) => p.role === 'shaft' || p.role === 'head';
const pads = show.drones.map((d) => ({ x: d.waypoints[0].x, y: d.waypoints[0].y }));
const padR = pads.map((p) => Math.hypot(p.x, p.y)), padA = pads.map((p) => Math.atan2(p.y, p.x));
const iRam = NAME.indexOf('Shri Ram'), iAdv = NAME.indexOf('Shri Ram advances'), iFire = NAME.indexOf('The baan is loosed');

// ---- light effects for the new motifs that use the formerly dark drones ----------------------------------------------------------------------------------
const sunCentre = {};
const getSunC = (k) => { if (!sunCentre[k]) { const c = forms[k].points.find((q) => q.role === 'sun' && Math.abs(q.ph - 1.08) < 1e-6); sunCentre[k] = c ? [c.x, c.alt] : [0, 0]; } return sunCentre[k]; };
const burstT0 = (nm) => (nm === 'Grand Finale' ? 12.0 : 9.4);
const extraIdle = (k, i, p, t, b) => {
  const r = p.role; if (!r) return null; const nm = NAME[k];
  switch (r) {
    case 'core': { const w = 0.5 + 0.5 * Math.sin(2 * Math.PI * (t / 2.4 + (p.ph - 1.04) * 2)); return scl(mix(b, WHITE, 0.45 * w), 0.6 + 0.4 * w); }          // the glowing core pulses
    case 'sun': case 'ray': {                                                                                                                                  // the sun: a pulse flows outward and a bright sweep circles it
      const c = getSunC(k), ang = Math.atan2(p.alt - c[1], p.x - c[0]), rr = Math.hypot(p.x - c[0], p.alt - c[1]);
      const sweep = Math.pow(Math.max(0, Math.cos(ang - 2 * Math.PI * t / 6)), 6), pulse = 0.65 + 0.35 * Math.sin(2 * Math.PI * (t / 3 - rr / 22));
      return mix(scl(b, pulse), WHITE, 0.55 * sweep);
    }
    case 'flag_pole': return scl([220, 220, 220], 0.7 + 0.3 * hash(i, Math.floor(t * 3)));
    case 'flag_cloth': { const w = 0.5 + 0.5 * Math.sin(2 * Math.PI * (t / 1.8 - p.x * 0.09)); return mix(scl(b, 0.65 + 0.35 * w), WHITE, 0.25 * w * w); }               // a ripple runs through the flag
    case 'lamp': {                                                                                                                                              // the toran: every lamp flickers, a glow travels along the string
      const heat = clamp01(0.4 + 0.6 * hash(i, Math.floor(t * 12))), wave = 0.5 + 0.5 * Math.sin(2 * Math.PI * (t / 3.4 - p.x / 40));
      return scl(ramp([[200, 60, 5], [255, 130, 20], [255, 200, 60], [255, 245, 180]], heat), 0.45 + 0.55 * wave);
    }
    case 'chakri': { const sp = Math.pow(Math.max(0, Math.sin(2 * Math.PI * (t / 1.4 - p.ph * 5))), 3); return mix(scl(b, 0.6 + 0.4 * hash(i, Math.floor(t * 8))), WHITE, 0.7 * sp); }   // sparks fly off the spinning chakri
    case 'burst': {
      const cs = forms[k].meta.bursts, c0 = cs.reduce((m, c) => (Math.hypot(p.x - c[0], p.alt - c[1]) < Math.hypot(p.x - m[0], p.alt - m[1]) ? c : m)), rho = Math.hypot(p.x - c0[0], p.alt - c0[1]) / 6.4;
      const ph7 = fract((t - burstT0(nm)) / 7 - (nm === 'Grand Finale' ? 0.11 : 0.22) * cs.indexOf(c0)), front = (ph7 / 0.34) * 1.4;
      if (ph7 < 0.34) { const w = Math.exp(-Math.pow((rho - front) / 0.32, 2)); return mix(scl(b, rho < front ? 0.9 : 0.25), WHITE, 0.92 * w); }
      const f = Math.pow(1 - (ph7 - 0.34) / 0.66, 1.4), tw = hash(i, Math.floor(t * 11)); return scl(b, 0.12 + 0.88 * f * (0.35 + 0.65 * tw));
    }
    case 'rocket': {
      const sidx = (p.ph - 1.04) / 0.05, chase = fract(t / 2.2) * 1.5 - 0.1;
      if (sidx < 0.5) return scl(mix([255, 90, 10], WHITE, hash(i, Math.floor(t * 16))), 0.55 + 0.45 * hash(i + 5, Math.floor(t * 20)));
      const w = Math.exp(-Math.pow((sidx / 1.0 - chase) / 0.22, 2)), nose = p.ph > 1.085 ? 0.5 + 0.5 * Math.max(0, Math.sin(2 * Math.PI * (t * 1.4 + p.x * 0.02))) : 0;
      return mix(scl(b, 0.75 + 0.25 * Math.sin(2 * Math.PI * t / 2.2)), WHITE, Math.max(0.75 * w, 0.6 * nose));
    }
    case 'diya_flame': {                                                                                                                                        // diya flames: lit one after another in the Diya scene, then they flicker
      const rank = (Math.abs(p.x) > 30 ? 2 : 0) + (p.x > 0 ? 1 : 0), gate = nm === 'Diya' ? smooth((t - (8.0 + 0.9 * rank)) / 0.9) : 1;
      const heat = clamp01(0.45 + 0.55 * hash(i, Math.floor(t * 13))); return scl(ramp([[200, 45, 5], [255, 110, 15], [255, 190, 40], [255, 245, 190]], heat), (0.6 + 0.4 * hash(i + 9, Math.floor(t * 17))) * (0.1 + 0.9 * gate));
    }
    case 'diya_bowl': case 'diya_rim': return scl(b, 0.8 + 0.2 * Math.sin(2 * Math.PI * t / 3 + p.x));
    case 'sparkle': { const tw = hash(i, Math.floor(t * 6)); return tw > 0.55 ? mix(b, WHITE, 0.7) : scl(b, 0.22 + 0.5 * tw); }
    default: return null;
  }
};

// ---- scene idle animations (full-brightness colour at local time t) ------------------------------------------------------
const idle = (k, i, t) => {
  const p = pt(k, i), b = base(k, i), nm = NAME[k], a1 = alt01(k, p);
  if (k !== iGan && k !== iHap) { const ex = extraIdle(k, i, p, t, b); if (ex) return ex; }
  switch (nm) {
    case 'Golden Spiral': return scl(ramp(SPIRAL_PAL, fract(p.ph - t * 0.10)), 0.85 + 0.15 * Math.sin(2 * Math.PI * (t * 0.5 + p.ph * 3)));
    case 'Diya': {
      const r = p.role;
      if (r === 'flame_tip' || r === 'flame_out' || r === 'flame_in' || r === 'core') {                     // the flame: a heat gradient that rises, with a travelling wave and a flicker
        const h = clamp01(p.ph / 0.22), inner = r === 'flame_in' || r === 'core';
        const sway = Math.sin(2 * Math.PI * (t * 0.45 + p.x * 0.05)) * 0.12;                                // the whole flame leans a little, left and right
        const wave = 0.5 + 0.5 * Math.sin(2 * Math.PI * (t * 1.7 - h * 1.35 + sway + hash(i, 1) * 0.12));   // brightness pulses climb the flame
        const flick = hash(i, Math.floor(t * 14)), slow = 0.5 + 0.5 * Math.sin(2 * Math.PI * (t / 2.6 + h * 0.4));
        const heat = clamp01(0.18 + 0.52 * h + (inner ? 0.24 : 0) + 0.26 * (wave - 0.5) + 0.22 * (flick - 0.5));
        const c = ramp([[190, 35, 4], [255, 92, 10], [255, 165, 28], [255, 232, 110], [255, 252, 220]], heat);
        if (r === 'flame_tip') return mix(c, WHITE, 0.35 + 0.65 * Math.max(0, Math.sin(2 * Math.PI * (t * 2.9 + 0.2))) ** 2);   // the tip flares white
        return scl(c, 0.52 + 0.30 * wave + 0.18 * (inner ? slow : flick));
      }
      const glow = 0.5 + 0.5 * Math.sin(2 * Math.PI * (t * 1.7 - 0.1));                                       // the bowl warms in time with the flame
      if (r === 'halo') { const d = Math.abs(fract(t * 0.2) - (p.ph - 0.7) / 0.3); const w = Math.exp(-Math.pow(Math.min(d, 1 - d) * 9, 2)); return scl(mix(b, WHITE, 0.55 * w), 0.55 + 0.45 * w); }   // a light runs along the halo
      if (r === 'lotus') { const w = Math.exp(-Math.pow((fract(t * 0.33) - (p.x + 16) / 32) * 5, 2)); return scl(mix(b, [255, 220, 240], 0.55 * w), 0.7 + 0.3 * w); }                       // a shimmer crosses the petals
      if (r === 'rim' || r === 'rim2') return scl(mix(b, [255, 200, 90], 0.22 * glow), 0.8 + 0.2 * (0.5 + 0.5 * Math.sin(2 * Math.PI * (t / 3 - p.x * 0.012))));
      return scl(mix(b, [255, 150, 60], 0.14 * glow), 0.8 + 0.2 * Math.sin(2 * Math.PI * t / 3.2 + p.x * 0.05));    // the bowl
    }
    case 'Happy Diwali': {
      const r = p.role;
      if (r === 'burst') {                                                                              // starbursts: a white wavefront flies outward, then the embers twinkle out
        const cs = forms[k].meta.bursts, c0 = cs.reduce((m, c) => (Math.hypot(p.x - c[0], p.alt - c[1]) < Math.hypot(p.x - m[0], p.alt - m[1]) ? c : m)), rho = Math.hypot(p.x - c0[0], p.alt - c0[1]) / 6.4;
        const ph7 = fract((t - 9.4) / 7 - (c0[0] > 0 ? 0.22 : 0)), front = (ph7 / 0.34) * 1.4;
        if (ph7 < 0.34) { const w = Math.exp(-Math.pow((rho - front) / 0.32, 2)); return mix(scl(b, rho < front ? 0.9 : 0.25), WHITE, 0.92 * w); }
        const f = Math.pow(1 - (ph7 - 0.34) / 0.66, 1.4), tw = hash(i, Math.floor(t * 11)); return scl(b, 0.12 + 0.88 * f * (0.35 + 0.65 * tw));
      }
      if (r === 'rocket') {                                                                             // rockets: the exhaust flickers and a spark runs up the body, nose flares
        const sidx = (p.ph - 1.04) / 0.05, chase = fract(t / 2.2) * 1.5 - 0.1;
        if (sidx < 0.5) return scl(mix([255, 90, 10], WHITE, hash(i, Math.floor(t * 16))), 0.55 + 0.45 * hash(i + 5, Math.floor(t * 20)));
        const w = Math.exp(-Math.pow((sidx / 1.0 - chase) / 0.22, 2)); const nose = p.ph > 1.085 ? 0.5 + 0.5 * Math.max(0, Math.sin(2 * Math.PI * (t * 1.4 + p.x * 0.02))) : 0;
        return mix(scl(b, 0.75 + 0.25 * Math.sin(2 * Math.PI * t / 2.2)), WHITE, Math.max(0.75 * w, 0.6 * nose));
      }
      if (r === 'fountain') {                                                                           // the fountain: sparks climb the rows and twinkle
        const row = clamp01((p.alt - 9.0) / 13.6), w = Math.exp(-Math.pow((row - fract(t / 1.5) * 1.3 + 0.1) / 0.2, 2)), tw = hash(i, Math.floor(t * 12));
        return tw > 0.82 ? WHITE : mix(scl(b, 0.5 + 0.5 * tw), WHITE, 0.8 * w);
      }
      if (p.ph < 0.47) return scl(b, 0.78 + 0.22 * hash(i, Math.floor(t * 3)));
      const xs = -66 + 132 * fract(t / 4.2), w = Math.exp(-Math.pow((p.x - xs) / 9, 2)); return mix(scl(b, 0.9), [255, 250, 225], 0.75 * w);                          // a gold shimmer sweeps through DIWALI
    }
    case 'Lord Ganesha': {
      if (p.role === 'lotus') {                                                                          // the lotus blooms: a pink wave runs from the centre to the petal tips
        const wv = 0.5 + 0.5 * Math.sin(2 * Math.PI * (t / GM.lotT - Math.abs(p.x) / 41 * GM.lotLag / GM.lotT));
        return scl(mix(b, [255, 205, 255], 0.38 * wv), 0.62 + 0.38 * wv);
      }
      if (p.role === 'swastik') return scl(mix(b, [255, 225, 120], 0.45 * (0.5 + 0.5 * Math.sin(2 * Math.PI * (t / 2.6 + p.ph * 3)))), 0.72 + 0.28 * hash(i, Math.floor(t * 4)));          // saffron, glowing
      if (p.role === 'diya_flame') { const heat = clamp01(0.45 + 0.55 * hash(i, Math.floor(t * 13))); return scl(ramp([[200, 45, 5], [255, 110, 15], [255, 190, 40], [255, 245, 190]], heat), 0.6 + 0.4 * hash(i + 9, Math.floor(t * 17))); }
      if (p.role === 'diya_bowl' || p.role === 'diya_rim') return scl(b, 0.8 + 0.2 * Math.sin(2 * Math.PI * t / 3 + p.x));
      if (p.role === 'sparkle') { const tw = hash(i, Math.floor(t * 6)); return tw > 0.55 ? mix(b, WHITE, 0.7) : scl(b, 0.22 + 0.5 * tw); }
      if (p.role === 'ear_L' || p.role === 'ear_R') {                                                    // ears: a pink glow pulses with every flap
        const lag = p.role === 'ear_R' ? GM.earLag : 0, pw = 0.5 + 0.5 * Math.sin(2 * Math.PI * (t - lag) / GM.earT);
        return scl(mix(b, [255, 190, 255], 0.35 * pw), 0.62 + 0.38 * pw);
      }
      if (p.role === 'hand') {                                                                           // the blessing hand: light runs from the wrist to the fingertips as it waves, fingertips sparkle
        const hw = clamp01((p.alt - 43) / 20), wv = 0.5 + 0.5 * Math.sin(2 * Math.PI * (t / GM.handT - hw * 0.35));
        const spark = Math.pow(Math.max(0, Math.sin(2 * Math.PI * (t / 2.3 - hw * 1.2))), 4);
        return mix(scl(b, 0.62 + 0.38 * wv), WHITE, 0.6 * spark);
      }
      if (p.r > 220 && p.g < 90) return scl(b, 0.7 + 0.3 * Math.sin(2 * Math.PI * t / 1.6));                                                                      // the tilak glows
      let c = scl(b, 0.74 + 0.26 * Math.cos(2 * Math.PI * (a1 - t / 5)));                                                                                           // a light wave rises through him
      if (a1 > 0.86 && hash(i, Math.floor(t * 9)) < 0.3) c = [255, 245, 215]; return c;                                                                            // the crown sparkles
    }
    case 'Shri Ram': case 'Shri Ram advances': case 'The baan is loosed': {
      if (isArrow(p)) return scl(mix(b, WHITE, 0.35), 0.8 + 0.2 * Math.sin(2 * Math.PI * (t * 1.3 - p.ph * 6)));
      if (p.role === 'bow' || p.role === 'string') return scl(b, 0.82 + 0.18 * Math.sin(2 * Math.PI * (t / 2.4) + p.ph * 5));
      return scl(b, 0.9 + 0.1 * Math.sin(2 * Math.PI * t / 4 + i));
    }
    case 'Ripple Rings': { const w = 0.5 + 0.5 * Math.sin(2 * Math.PI * (p.ph * 2.2 - t * 0.55)); return scl(b, 0.4 + 0.6 * w); }                                       // ripples run outward
    case 'Rangoli': { const a = Math.atan2(p.alt - aMid[k], p.x), w = 0.5 + 0.5 * Math.cos(4 * a - t * 1.4); return mix(scl(b, 0.5 + 0.5 * w), WHITE, 0.25 * w * w); }   // a rotating wedge of light
    case 'Firecrackers & Sparkles': {
      const s = hash(i, Math.floor(t * 9)); if (p.ph < 0.30) { const w = Math.exp(-Math.pow((a1 - fract(t * 0.5)) * 6, 2)); return mix(scl(b, 0.7), WHITE, 0.7 * w); }   // fuse light climbs
      return s > 0.93 ? WHITE : scl(b, 0.45 + 0.55 * s);                                                                                                            // the bursts twinkle
    }
    case 'Ayodhya Mandir': {
      if (p.role === 'mpole') return scl([255, 160, 40], 0.85 + 0.15 * Math.sin(2 * Math.PI * t / 3));
      if (p.role === 'mflag') {                                                                           // the flag: a flame-coloured wave runs from the pole to the tip as it flutters
        const dist = p.x - forms[k].meta.flag_pole_x, heat = clamp01(0.55 + 0.45 * Math.sin(2 * Math.PI * (t / 1.6 - dist / 9)) + 0.12 * (hash(i, Math.floor(t * 9)) - 0.5));
        return scl(ramp([[170, 15, 0], [255, 40, 5], [255, 120, 20], [255, 190, 60]], heat), 0.8 + 0.2 * Math.sin(2 * Math.PI * (t / 1.6 - dist / 9)));
      }
      if (a1 > 0.85 && p.r > 200 && p.g < 140) return mix([255, 60, 10], [255, 170, 30], hash(i, Math.floor(t * 8)));                                               // the flag flutters
      const w = Math.exp(-Math.pow((a1 - fract(t / 5)) / 0.07, 2)); return mix(scl(b, 0.85), [255, 245, 200], 0.6 * w);                                            // light climbs the spires
    }
    case 'Lakshmi with Lotus': {
      if (p.role === 'eye') return fract(t / 3.6) < 0.06 ? BLACK : scl(b, 0.9 + 0.1 * Math.sin(2 * Math.PI * t / 2.2));                                  // the eyes blink
      if (p.role === 'crown') { const tw = hash(i, Math.floor(t * 7)); return tw > 0.8 ? WHITE : scl(mix(b, [255, 245, 200], 0.3), 0.7 + 0.3 * tw); }       // the crown jewels sparkle
      if (p.role === 'halo') {                                                                                                                              // the halo: a radiant pulse and a bright sweep around it
        const ang = Math.atan2(p.alt - 96, p.x), sw = Math.pow(Math.max(0, Math.cos(ang - 2 * Math.PI * t / 5)), 5), pulse = 0.5 - 0.5 * Math.cos(2 * Math.PI * (t - LK.T0) / LK.haloT);
        return mix(scl(b, 0.6 + 0.4 * pulse), WHITE, 0.6 * sw);
      }
      if (p.role === 'hlotus') { const bl = 0.5 - 0.5 * Math.cos(2 * Math.PI * (t - LK.T0) / LK.lotT); return scl(mix(b, [255, 215, 255], 0.4 * bl), 0.65 + 0.35 * bl); }  // the hand lotuses glow as they bloom
      if (p.role === 'seat') { const wv = 0.5 + 0.5 * Math.sin(2 * Math.PI * (t / LK.seatT - Math.abs(p.x) / 50.6 * LK.seatLag / LK.seatT)); return scl(mix(b, [255, 205, 255], 0.38 * wv), 0.62 + 0.38 * wv); }
      if (p.role === 'saree') { const wv = 0.5 + 0.5 * Math.sin(2 * Math.PI * (t / 3.2 - a1 * 1.4)); return mix(scl(b, 0.6 + 0.4 * wv), [255, 230, 150], 0.25 * wv * wv); }   // the saree flows
      if (p.role === 'lhand') {                                                                                                                             // golden coins trickle down the lower hands
        const front = fract(t / 2.4), d = Math.abs((1 - c01((p.alt - 35) / 27)) - front), coin = Math.exp(-Math.pow(d / 0.12, 2)); return mix(scl(b, 0.7), [255, 225, 90], 0.8 * coin);
      }
      const g = hash(i, Math.floor(t * 6)); if (g > 0.95) return WHITE;
      if (a1 > 0.84) return scl(b, 0.65 + 0.35 * Math.sin(2 * Math.PI * t / 2.6));                                                                                  // the halo pulses
      return scl(b, 0.75 + 0.25 * Math.sin(2 * Math.PI * (t / 3.4 + a1)));
    }
    case 'Grand Finale': {
      let c = scl(b, 0.55 + 0.45 * hash(i, Math.floor(t * 8)));
      const hubs = forms[k].meta.hubs || []; for (const h of hubs) { const d = Math.hypot(p.x - h[0], p.alt - h[1]), r = (t % 4.5) * 16; if (Math.abs(d - r) < 4.5 && r < 30) c = WHITE; }    // bursts flash outward
      if (p.ph < 0.22) { const w = Math.exp(-Math.pow((p.x - (-60 + 120 * fract(t / 5))) / 8, 2)); c = mix(scl(b, 0.7), WHITE, 0.6 * w); }                           // lights run along the skyline
      else if (p.ph < 0.5) c = scl(b, 0.8 + 0.2 * Math.sin(2 * Math.PI * t / 2.5));
      return c;
    }
    default: return b;
  }
};

// ---- the light plan --------------------------------------------------------------------------------------------------------------
const T_LIT = 40;                                                  // takeoff + the start of the dispersion stay visible this long, then the lights go out
const launch = (i, t) => {
  if (t >= T_LIT) return BLACK;
  const on = smooth((t - padR[i] / 22) / 1.8), fade = 1 - smooth((t - (T_LIT - 3)) / 3);                          // the pads light up from the centre outward, then fade
  const c = hsv(fract(padR[i] / 90 + t * 0.10 + 0.25 * Math.sin(padA[i] * 3)), 0.85, 1);
  return scl(mix(c, WARM, smooth((t - 14) / 10) * 0.5), on * fade);
};
const DIS_ON = 4.5, DIS_FADE = 2.5;
const dissolve = (kp, i, local) => {                                // leaving a picture: the lights stay on while the drones scatter, then fade out
  if (!lit(kp, i)) return BLACK;
  const c0 = idle(kp, i, HOLD[NAME[kp]]);
  if (local < DIS_ON) return scl(mix(c0, [255, 238, 205], 0.5 * smooth(local / 1.5)), 0.85 + 0.15 * hash(i, Math.floor(local * 12)));
  const f = 1 - smooth((local - DIS_ON) / DIS_FADE); if (f <= 0) return BLACK;
  return scl(mix(c0, [255, 238, 205], 0.5), f * (0.8 + 0.2 * hash(i, Math.floor(local * 14))));
};
const returnHome = (i, t, g) => {
  const local = t - g.t0; if (local < DIS_ON + DIS_FADE) return dissolve(K - 1, i, local);
  const t_home = g.t1 - 30; if (t < t_home) return BLACK;
  const f = smooth((t - t_home) / 5), wave = 0.6 + 0.4 * Math.sin(2 * Math.PI * (t * 0.35 - padR[i] / 40)); return scl(WARM, f * wave * 0.85);          // a warm glow as they come home
};
const landing = (i, t, g) => { const u = (t - g.t0) / Math.max(1e-6, g.t1 - g.t0), wave = 0.6 + 0.4 * Math.sin(2 * Math.PI * (t * 0.35 - padR[i] / 40)); return scl(WARM, 0.85 * wave * (1 - smooth(u))); };
const holdColour = (k, i, local) => {
  if (!lit(k, i)) return BLACK;
  if (KIND[k] === 'lit') {                                         // the baan group keeps its lights on: no dark flight, no reveal
    const p = pt(k, i);
    if (k === iFire && isArrow(p)) { const fade = 1 - 0.55 * smooth(local / HOLD[NAME[k]]); return scl(mix(WHITE, GOLD, 0.35), (0.55 + 0.45 * ((p.ph - 0.88) / 0.12)) * fade * (0.85 + 0.15 * hash(i, Math.floor(local * 14)))); }
    return idle(k, i, local + HOLD[NAME[iRam]]);
  }
  const p = pt(k, i), tOn = p.ph * REV[NAME[k]];
  if (local < tOn) return BLACK;
  const age = local - tOn, c = idle(k, i, local);
  if (age < 0.6) return mix(scl(WHITE, 0.6 + 0.4 * smooth(age / 0.3)), c, smooth(age / 0.6));                      // each drone ignites with a white pop, then settles to its colour
  if (NAME[k] === 'Grand Finale' && local > HOLD[NAME[k]] - 4.2) return Math.floor(local * 5) % 2 === 0 ? WHITE : c; // the finale ends in a strobe
  return c;
};
const litTransition = (k, i, local, u) => {                          // morphs into Shri Ram advances / the baan: lights stay on
  if (!lit(k, i)) return BLACK;
  const p = pt(k, i);
  if (k === iAdv) return mix(idle(iRam, i, HOLD[NAME[iRam]] + local), GOLD, 0.18 * (0.5 + 0.5 * Math.sin(2 * Math.PI * local / 3)));      // a warm golden pulse as he steps forward
  const f = Math.exp(-local / 0.4);                                                                                                      // release flash
  if (isArrow(p)) { const tail = clamp01((p.ph - 0.88) / 0.12); return scl(mix(WHITE, GOLD, 0.35 * (1 - tail)), (0.5 + 0.5 * tail) * (0.8 + 0.2 * hash(i, Math.floor(local * 16)))); }
  return mix(idle(iAdv, i, HOLD[NAME[iAdv]] + local), WHITE, 0.55 * f);
};
let segPtr = new Array(N).fill(0);
const colourAt = (i, t) => {
  let g = S[segPtr[i]];
  while (t >= g.t1 && segPtr[i] < S.length - 1) { segPtr[i]++; g = S[segPtr[i]]; }
  const u = clamp01((t - g.t0) / Math.max(1e-6, g.t1 - g.t0)), local = t - g.t0;
  if (g.kind === 'takeoff' || g.label === 'Climb-out') return launch(i, t);
  if (g.kind === 'land') return landing(i, t, g);
  if (g.label === 'Return') return returnHome(i, t, g);
  if (g.kind === 'hold') return holdColour(g.k, i, local);
  return KIND[g.k] === 'lit' ? litTransition(g.k, i, local, u) : dissolve(g.k - 1, i, local);       // a morph into scene g.k
};
for (let i = 0; i < N; i++) for (const w of show.drones[i].waypoints) {
  const c = colourAt(i, w.time); w.r = Math.round(clamp01(c[0] / 255) * 255); w.g = Math.round(clamp01(c[1] / 255) * 255); w.b = Math.round(clamp01(c[2] / 255) * 255); w.w = 0;
}

// ---- checks: the baan really is the same drones -----------------------------------------------------------------------------
const at = (i, t) => { const wps = show.drones[i].waypoints; let lo = 0, hi = wps.length - 1; while (hi - lo > 1) { const m = (lo + hi) >> 1; if (wps[m].time <= t) lo = m; else hi = m; } return wps[lo]; };
const holdOf = (k) => S.find((g) => g.kind === 'hold' && g.k === k);
let moved0 = 0, movedA = 0, nA = 0, maxErr = 0; const A = forms[iAdv].meta.adv_m, D = forms[iFire].meta.fire_m;
for (let i = 0; i < N; i++) {
  if (!lit(iRam, i)) continue;
  const p0 = at(i, holdOf(iRam).t1 - 0.5), p1 = at(i, holdOf(iAdv).t1 - 0.05), p2 = at(i, holdOf(iFire).t0 + 0.5);
  const arrow = isArrow(pt(iRam, i)); const dx1 = p1.x - p0.x, dx2 = p2.x - p1.x;
  maxErr = Math.max(maxErr, Math.abs(dx1 - A), Math.abs(p1.y - p0.y), Math.abs(arrow ? dx2 - D : dx2), Math.abs(p2.y - p1.y));
  if (arrow) { nA++; if (Math.abs(dx2 - D) < 0.6) movedA++; } else if (Math.abs(dx2) < 0.6) moved0++;
}
console.log(`baan check: ${movedA}/${nA} arrow drones moved ${D.toFixed(1)} m forward, ${moved0} other lit drones stayed put; advance ${A} m rigid; max deviation ${maxErr.toFixed(2)} m`);

fs.mkdirSync(outDir, { recursive: true });
fs.writeFileSync(path.join(outDir, 'timeline.json'), JSON.stringify({ segments: S, names: NAME, kinds: KIND, holds: NAME.map((n) => HOLD[n] ?? 14), trans: NAME.map((n) => TRANS[n] ?? 12), duration_s: show._duration }, null, 1));
fs.writeFileSync(path.join(outDir, 'target_idx.json'), JSON.stringify({ targetIdx: show._targetIdx, duration: show._duration }));
const choreo = { site: SITE, drones: show.drones };
fs.writeFileSync(path.join(outDir, 'choreo.json'), JSON.stringify(choreo));
console.log(`built: fleet ${N}, ${NAME.length} scenes, ${show._duration} s, ${show.drones[0].waypoints.length} waypoints per drone`);
const r = await fetch(`${api}/api/validate`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(choreo) }); const v = await r.json().catch(() => ({}));
const result = { fleet: N, duration_s: show._duration, http: r.status, validator: { status: v.status, feasibility_certified: v.feasibility_certified, metrics: v.metrics, violation_counts: Object.fromEntries(Object.entries(v.violations ?? {}).map(([k, x]) => [k, x.length])) } };
fs.writeFileSync(path.join(outDir, 'result.json'), JSON.stringify(result, null, 2)); console.log(JSON.stringify(result));
