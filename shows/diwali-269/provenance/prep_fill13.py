# ruff: noqa: E401, E402, E701, E702, F811  (analysis script that records how this show was made; not product code)
"""PROVENANCE ONLY. Needs the earlier scene folders (formations_v12/) that are not in this repo; the final result of the chain is ../formations/. Kept to show how every spare drone was assigned a motif / how each moving part was labelled."""
"""Give every scene that still has dark (spare) drones something to do with them.  formations_v13 = formations_v12 with only the scenes below
rewritten (their lit points are untouched, the spare drones become motifs).  Everything that is not listed is copied byte-for-byte.

  1 Golden Spiral   : a 2-drone core in the middle of the spiral
  2 Diya            : 4 floating diyas inside the halo (+ sparkles)
  5,6,7 Shri Ram    : a sun with rays (top right) and a dharma flag (bottom right) (+ sparkles)  [same spare positions in all three scenes]
  8 Ripple Rings    : a 7-drone flower in the centre (+ sparkles)
  9 Rangoli         : 2 diyas at the bottom corners (+ sparkles)
  10 Firecrackers   : 2 more rockets, 2 spinning chakris (+ sparkles)
  11 Ayodhya Mandir : a toran: a string of 28 lamps across the top
  13 Grand Finale   : 3 starbursts across the top (+ sparkles)
Every drone placed is >= 2.88 m from every other drone of its scene and inside the site / height limits.   python prep_fill13.py"""
import json, os, shutil, numpy as np
from scipy.spatial import cKDTree
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt

import os
HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', ''); SRC = HERE + 'formations_v12/'; OUT = HERE + 'formations_v13/'
if os.path.exists(OUT): shutil.rmtree(OUT)
shutil.copytree(SRC, OUT)
ALT_MIN, ALT_MAX, X_MAX, GAP = 8.0, 112.0, 57.5, 2.9
SUM = json.load(open(SRC + 'summary.json')); N = SUM['fleet']
GOLD, ORANGE, SAFF, MAG, WHITE, VIOLET, CYAN, BLUE, RED, GREEN, PINK = (255, 205, 50), (255, 105, 20), (255, 140, 25), (240, 70, 210), (255, 245, 220), (170, 90, 245), (60, 220, 255), (60, 130, 255), (255, 55, 40), (70, 240, 130), (255, 110, 190)

def curve_pts(P, closed=False, step=3.08):
    P = np.asarray(P, float); Q = np.vstack([P, P[:1]]) if closed else P
    seg = np.hypot(*np.diff(Q, axis=0).T); L = np.concatenate([[0], np.cumsum(seg)]); n = max(1, int(L[-1] // step)); h = L[-1] / n
    ts = np.arange(n if closed else n + 1) * h; return np.column_stack([np.interp(ts, L, Q[:, 0]), np.interp(ts, L, Q[:, 1])])

# ---- motifs: lists of (dx, dalt, colour, role, ph)  (relative to the motif's anchor) ----------------------------------------------------------------
def m_burst(pal, ph0=1.30):
    r = [(0.0, 0.0, pal[0], 'burst', ph0)]
    r += [(3.4 * np.cos(a), 3.4 * np.sin(a), pal[1], 'burst', ph0 + 0.04) for a in np.radians(np.arange(0, 360, 60))]
    r += [(6.4 * np.cos(a), 6.4 * np.sin(a), pal[2], 'burst', ph0 + 0.10) for a in np.radians(np.arange(15, 375, 30))]
    return r
def m_diya(ph0=1.10):
    a_, b_ = 5.0, 3.9; r = []; seen = []
    def put(x, y, c, role, ph):
        if seen and min(np.hypot(x - sx, y - sy) for sx, sy in seen) < 2.88: return
        seen.append((x, y)); r.append((x, y, c, role, ph))
    th = np.linspace(0, np.pi, 200)
    for q in curve_pts(np.column_stack([a_ * np.cos(th), -b_ * np.sin(th)])): put(q[0], q[1], MAG, 'diya_bowl', ph0)
    for q in curve_pts(np.column_stack([np.linspace(-a_, a_, 50), np.zeros(50)])): put(q[0], q[1], GOLD, 'diya_rim', ph0 + 0.02)
    put(0.0, 3.1 + 9.2, WHITE, 'diya_flame', ph0 + 0.04)
    phi = np.linspace(0, 2 * np.pi, 400, endpoint=False)
    for q in curve_pts(np.column_stack([2.6 * np.sin(phi) * np.abs(np.sin(phi / 2)) ** 0.8, 3.1 + 4.6 + 4.6 * np.cos(phi)]), True): put(q[0], q[1], ORANGE, 'diya_flame', ph0 + 0.04)
    return r
def m_rocket(tilt, ph0=1.04):
    th = np.radians(tilt); u = np.array([np.sin(th), np.cos(th)]); n = np.array([np.cos(th), -np.sin(th)]); r = [(*(-u * 3.1), ORANGE, 'rocket', ph0)]
    for k, s in enumerate((0.0, 3.1, 6.2, 9.3)): r.append((*(u * s), RED if k % 2 == 0 else WHITE, 'rocket', ph0 + 0.01 + 0.01 * k))
    r.append((*(u * 12.4), GOLD, 'rocket', ph0 + 0.05))
    for sg in (-1, 1): r.append((*(u * 3.1 + n * sg * 3.1), BLUE, 'rocket', ph0 + 0.02))
    return r
def m_chakri(col, ph0=1.12):
    r = [(0.0, 0.0, WHITE, 'chakri', ph0)]
    for a in (90, 210, 330):
        for k, rad in enumerate((3.1, 6.2)): ang = np.radians(a + 22 * k); r.append((rad * np.cos(ang), rad * np.sin(ang), col, 'chakri', ph0 + 0.03 * (k + 1)))
    return r
def m_flower(ph0=1.10):
    r = [(0.0, 0.0, WHITE, 'core', ph0)]
    for a in np.radians(np.arange(0, 360, 60)): r.append((3.4 * np.cos(a), 3.4 * np.sin(a), GOLD, 'core', ph0 + 0.04))
    return r
def m_sun(ph0=1.08):
    r = [(0.0, 0.0, WHITE, 'sun', ph0)]
    r += [(4.4 * np.cos(a), 4.4 * np.sin(a), GOLD, 'sun', ph0 + 0.03) for a in np.radians(np.arange(0, 360, 60))]
    r += [(8.6 * np.cos(a), 8.6 * np.sin(a), ORANGE, 'sun', ph0 + 0.06) for a in np.radians(np.arange(0, 360, 22.5))]
    r += [(12.8 * np.cos(a), 12.8 * np.sin(a), GOLD, 'ray', ph0 + 0.09) for a in np.radians(np.arange(11.25, 371.25, 22.5))]
    r += [(16.2 * np.cos(a), 16.2 * np.sin(a), WHITE, 'ray', ph0 + 0.12) for a in np.radians(np.arange(11.25, 371.25, 45))]
    return r
def m_flag(ph0=1.14):
    r = [(0.0, 3.0 * k, (200, 200, 200), 'flag_pole', ph0) for k in range(8)]
    top = 24.0
    for q in curve_pts(np.array([[0.0, top], [11.5, top - 6.0], [0.0, top - 12.0]])): r.append((q[0], q[1], SAFF, 'flag_cloth', ph0 + 0.03))
    r.append((3.5, top - 6.0, WHITE, 'flag_cloth', ph0 + 0.05))
    out = []
    for p in r:                                   # the pennant closes back onto the pole: drop any cloth drone that lands on / next to a pole drone
        if all(np.hypot(p[0] - q[0], p[1] - q[1]) >= 2.88 for q in out): out.append(p)
    return out

class Scene:
    def __init__(self, avoid_sets):
        self.avoid = np.vstack(avoid_sets) if avoid_sets else np.zeros((0, 2)); self.rows = []; self.meta = {}
    def _tree(self):
        pts = np.vstack([self.avoid, np.array([(r[0], r[1]) for r in self.rows])]) if self.rows else self.avoid
        return cKDTree(pts) if len(pts) else None
    def fits(self, rel, anchor):
        P = np.array([(anchor[0] + dx, anchor[1] + da) for dx, da, *_ in rel])
        if (np.abs(P[:, 0]) > X_MAX).any() or (P[:, 1] < ALT_MIN).any() or (P[:, 1] > ALT_MAX).any(): return None
        t = self._tree(); clear = t.query(P)[0].min() if t is not None else 99.0
        d = cKDTree(P).query(P, k=2)[0][:, 1].min() if len(P) > 1 else 99.0
        return clear if (clear >= GAP and d >= 2.88) else None
    def place(self, rel, candidates):
        best = None
        for c in candidates:
            cl = self.fits(rel, c)
            if cl is not None and (best is None or cl > best[0]): best = (cl, c)
        if best is None: return None
        self.rows += [(best[1][0] + dx, best[1][1] + da, col, role, ph, '') for dx, da, col, role, ph in rel]; return best[1]
    def place_first(self, rel, candidates):
        for c in candidates:
            if self.fits(rel, c) is not None: self.rows += [(c[0] + dx, c[1] + da, col, role, ph, '') for dx, da, col, role, ph in rel]; return c
        return None
    def grid(self, step=2.5): return [(x, a) for x in np.arange(-55.0, 55.1, step) for a in np.arange(10.0, 110.1, step)]
    def sparkles(self, n, ph0=1.30):
        """leftover drones become small star groups (5 = a cross, 4 = a diamond, 3 = a triangle, 2 = a pair), never loose scattered dots"""
        STAR = {5: [(0, 0), (0, 3.1), (0, -3.1), (3.1, 0), (-3.1, 0)], 4: [(0, 3.1), (0, -3.1), (3.1, 0), (-3.1, 0)], 3: [(0, 2.0), (-2.9, -1.6), (2.9, -1.6)], 2: [(-1.55, 0), (1.55, 0)], 1: [(0, 0)]}
        k = 0
        while n > 0:
            sz = min(5, n); rel = [(dx, da, WHITE if (dx, da) == (0, 0) or k % 2 else GOLD, 'sparkle', ph0 + 0.02 * (k % 6)) for dx, da in STAR[sz]]
            c = self.place(rel, self.grid(1.5))
            if c is None and sz > 1: sz -= 1; rel = [(dx, da, GOLD, 'sparkle', ph0 + 0.02 * (k % 6)) for dx, da in STAR[sz]]; c = self.place(rel, self.grid(1.5))
            if c is None: raise SystemExit(f'no room for {n} more star drones')
            n -= sz; k += 1
    def count(self): return len(self.rows)

report = []
def write_scene(si, sc, name, extra_meta=None):
    f = json.load(open(SRC + f's{si:02d}.json')); nf = f['meta']['n_fig']; lit = f['points'][:nf]; n_new = N - nf
    assert sc.count() == n_new, f'{name}: placed {sc.count()} of {n_new} spare drones'
    Mn = np.array([(r[0], r[1]) for r in sc.rows]); Ml = np.array([[p['x'], p['alt']] for p in lit]); allP = np.vstack([Ml, Mn]); d = cKDTree(allP).query(allP, k=2)[0][:, 1]
    assert d.min() >= 2.85 - 1e-6, (name, d.min()); assert (np.abs(Mn[:, 0]) <= X_MAX + 1e-6).all() and (Mn[:, 1] >= ALT_MIN - 1e-6).all() and (Mn[:, 1] <= ALT_MAX + 1e-6).all(), name
    pts = list(lit)
    for x, a, col, role, ph, g in sc.rows: pts.append({'x': round(float(x), 3), 'y': 0.0, 'alt': round(float(a), 3), 'r': int(col[0]), 'g': int(col[1]), 'b': int(col[2]), 'ph': round(float(ph), 4), 'role': role, 'grp': g})
    meta = dict(f['meta']); meta['n_fig'] = N; meta.update(sc.meta); meta.update(extra_meta or {})
    json.dump({'points': pts, 'meta': meta}, open(OUT + f's{si:02d}.json', 'w'))
    sm = json.load(open(OUT + 'summary.json'))
    for s in sm['scenes']:
        if s['scene'] == name: s.update(lit=N, off=0, closest_all=round(float(d.min()), 2))
    json.dump(sm, open(OUT + 'summary.json', 'w'), indent=1)
    roles = [r[3] for r in sc.rows]; report.append((si, name, n_new, {r: roles.count(r) for r in dict.fromkeys(roles)}, float(d.min())))
    return Mn
def lit_of(si):
    f = json.load(open(SRC + f's{si:02d}.json')); return np.array([[p['x'], p['alt']] for p in f['points'][:f['meta']['n_fig']]])
def nspare(si): return N - json.load(open(SRC + f's{si:02d}.json'))['meta']['n_fig']

# ---- 1 Golden Spiral: a 2-drone core ----------------------------------------------------------------------------------------------------------------
sc = Scene([lit_of(1)]); sc.rows += [(-1.5, 60.0, WHITE, 'core', 1.05, ''), (1.5, 60.0, GOLD, 'core', 1.05, '')]; write_scene(1, sc, 'Golden Spiral')
# ---- 2 Diya: four floating diyas inside the halo + sparkles ------------------------------------------------------------------------------------------
sc = Scene([lit_of(2)])
for c in ((-23.0, 46.0), (23.0, 46.0), (-37.0, 62.0), (37.0, 62.0)): assert sc.place_first(m_diya(1.10 + 0.0), [c]) is not None, f'diya at {c}'
for i, r in enumerate(sc.rows): pass
sc.sparkles(nspare(2) - sc.count(), 1.30); write_scene(2, sc, 'Diya')
# ---- 5,6,7 Shri Ram: a sun and a flag, the SAME spare drones in all three scenes (so they stay put while Ram advances and looses the baan) -----------------
Lr = [lit_of(5), lit_of(6), lit_of(7)]; f5 = json.load(open(SRC + 's05.json')); f7 = json.load(open(SRC + 's07.json'))
ai = [i for i, p in enumerate(f5['points'][:f5['meta']['n_fig']]) if p['role'] in ('shaft', 'head')]; D = float(f7['meta']['fire_m']); ADV = float(f5['meta'].get('adv_m', 5.0) if 'adv_m' in f5['meta'] else 5.0)
path = np.vstack([np.array([[Lr[0][i, 0] + ADV + t * D, Lr[0][i, 1]] for i in ai]) for t in np.linspace(0, 1, 16)])
sc = Scene(Lr + [path])
suncs = [(x, a) for x in np.arange(30.0, 41.1, 1.0) for a in np.arange(92.0, 96.1, 1.0)]
assert sc.place(m_sun(), suncs) is not None, 'no room for the sun'
assert sc.place(m_flag(), [(x, a) for x in np.arange(34.0, 46.1, 1.0) for a in np.arange(9.0, 16.1, 1.0)]) is not None, 'no room for the flag'
sc.sparkles(nspare(5) - sc.count(), 1.30)
spare_rows = list(sc.rows)
for si in (5, 6, 7):
    s2 = Scene([Lr[si - 5]]); s2.rows = list(spare_rows); write_scene(si, s2, ['Shri Ram', 'Shri Ram advances', 'The baan is loosed'][si - 5])
# ---- 8 Ripple Rings: a flower in the centre + sparkles --------------------------------------------------------------------------------------------------
sc = Scene([lit_of(8)]); assert sc.place_first(m_flower(), [(0.0, 60.0)]) is not None; sc.sparkles(nspare(8) - sc.count(), 1.30); write_scene(8, sc, 'Ripple Rings')
# ---- 9 Rangoli: two diyas at the bottom corners + sparkles ------------------------------------------------------------------------------------------------
sc = Scene([lit_of(9)]); cs = [(-48.0, 12.0), (48.0, 12.0)]
for c in cs:
    got = sc.place(m_diya(1.10), [(c[0] + dx, c[1] + da) for dx in np.arange(-4, 4.1, 1.0) for da in np.arange(0, 6.1, 1.0)]); assert got is not None, f'rangoli diya near {c}'
sc.sparkles(nspare(9) - sc.count(), 1.30); write_scene(9, sc, 'Rangoli')
# ---- 10 Firecrackers: 2 rockets, 2 chakris, sparkles -------------------------------------------------------------------------------------------------------
sc = Scene([lit_of(10)]); ch = []
for tilt in (-14.0, 14.0):
    c = sc.place(m_rocket(tilt, 1.04), sc.grid(2.0)); assert c is not None, 'no room for a rocket'
for col in (CYAN, GREEN):
    c = sc.place(m_chakri(col, 1.12), [g for g in sc.grid(2.0) if 15.0 <= g[1] <= 104.0 and abs(g[0]) <= 50.0]); assert c is not None, 'no room for a chakri'; ch.append([float(c[0]), float(c[1])])
sc.meta['chakris'] = ch; sc.sparkles(nspare(10) - sc.count(), 1.30); write_scene(10, sc, 'Firecrackers & Sparkles')
# ---- 11 Ayodhya Mandir: a toran of 28 lamps across the top --------------------------------------------------------------------------------------------------
sc = Scene([lit_of(11)]); n_t = nspare(11); done = False
for W in np.arange(40.0, 57.4, 0.05):
    xs = np.linspace(-W, W, 400); ys = 109.0 - 4.2 * (1 - (xs / W) ** 2); L = np.cumsum(np.hypot(np.diff(xs), np.diff(ys))); n = int(L[-1] // 3.0)
    if n + 1 == n_t:
        Lc = np.concatenate([[0.0], L]); ts = np.linspace(0, Lc[-1], n_t); P = np.column_stack([np.interp(ts, Lc, xs), np.interp(ts, Lc, ys)]); done = True; break
assert done, 'cannot fit the toran'
sc.rows += [(q[0], q[1], ORANGE if k % 2 == 0 else GOLD, 'lamp', 1.04 + 0.34 * abs(q[0]) / 57.5, '') for k, q in enumerate(P)]; write_scene(11, sc, 'Ayodhya Mandir')
# ---- 13 Grand Finale: 3 starbursts across the top + sparkles -----------------------------------------------------------------------------------------------
sc = Scene([lit_of(13)]); bs = []
for cx, pal in ((-38.0, [GOLD, ORANGE, MAG]), (0.0, [WHITE, GREEN, CYAN]), (38.0, [WHITE, CYAN, BLUE])):
    got = sc.place(m_burst(pal, 1.30), [(cx + dx, 101.5 + da) for dx in np.arange(-3, 3.1, 1.0) for da in np.arange(-2, 2.1, 1.0)]); assert got is not None, f'finale burst at {cx}'; bs.append([float(got[0]), float(got[1])])
sc.meta['bursts'] = bs; sc.sparkles(nspare(13) - sc.count(), 1.40); write_scene(13, sc, 'Grand Finale', {'bursts_extra': bs})
# the Happy Diwali / Firecracker burst bookkeeping used elsewhere stays in their own meta

print('scene  name                      spare  placed'); [print(f'{si:>3}    {nm[:24]:24} {n:>5}  {roles}  closest {dm:.2f} m') for si, nm, n, roles, dm in report]
# preview sheet of every changed scene
names = {1: 'Golden Spiral', 2: 'Diya', 5: 'Shri Ram', 7: 'The baan is loosed', 8: 'Ripple Rings', 9: 'Rangoli', 10: 'Firecrackers & Sparkles', 11: 'Ayodhya Mandir', 13: 'Grand Finale'}
fig, ax = plt.subplots(3, 3, figsize=(27, 27), facecolor='#04050b')
for a, (si, nm) in zip(ax.flat, names.items()):
    f = json.load(open(OUT + f's{si:02d}.json')); P = f['points']; nf0 = json.load(open(SRC + f's{si:02d}.json'))['meta']['n_fig']; a.set_facecolor('#04050b')
    a.scatter([p['x'] for p in P[:nf0]], [p['alt'] for p in P[:nf0]], s=26, c=[[p['r'] / 255, p['g'] / 255, p['b'] / 255] for p in P[:nf0]])
    a.scatter([p['x'] for p in P[nf0:]], [p['alt'] for p in P[nf0:]], s=50, c=[[p['r'] / 255, p['g'] / 255, p['b'] / 255] for p in P[nf0:]], edgecolors='#ffffff', linewidths=0.6)
    a.set_aspect('equal'); a.set_xlim(-62, 62); a.set_ylim(0, 116); a.axis('off'); a.set_title(f'{nm}: {nf0} + {len(P) - nf0} new drones (white-ringed)', color='#ffd9a0', fontsize=14)
plt.tight_layout(); plt.savefig(HERE + 'fill_all_scenes.png', dpi=36, facecolor='#04050b')
