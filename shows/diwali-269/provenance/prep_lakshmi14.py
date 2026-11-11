# ruff: noqa: E401, E402, E701, E702, F811  (analysis script that records how this show was made; not product code)
"""PROVENANCE ONLY. Needs the earlier scene folders (formations_v13/) that are not in this repo; the final result of the chain is ../formations/. Kept to show how every spare drone was assigned a motif / how each moving part was labelled."""
"""Label the parts that will move: the Mandir flag (s11) and Lakshmi's body parts (s12).  Positions are NOT changed.
formations_v14 = formations_v13 with only s11 and s12 relabelled (+ meta); every other scene file is copied byte-for-byte.   python prep_lakshmi14.py"""
import json, os, shutil, numpy as np
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
import os
HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', ''); SRC = HERE + 'formations_v13/'; OUT = HERE + 'formations_v14/'
if os.path.exists(OUT): shutil.rmtree(OUT)
shutil.copytree(SRC, OUT)
# ---- Mandir: the pennant (6 red drones) and the two pole drones ------------------------------------------------------------------------------------------
f = json.load(open(SRC + 's11.json')); nf = f['meta']['n_fig']
for p in f['points'][:nf]:
    if p['alt'] > 91 and p['role'] == '':
        p['role'] = 'mflag' if (p['r'] > 200 and p['g'] < 80) else 'mpole'
roles = [p['role'] for p in f['points'][:nf]]; print('Mandir flag: cloth', roles.count('mflag'), 'pole', roles.count('mpole'))
f['meta']['flag_pole_x'] = 0.5
json.dump(f, open(OUT + 's11.json', 'w'))
# ---- Lakshmi -----------------------------------------------------------------------------------------------------------------------------------------------------
g = json.load(open(SRC + 's12.json')); ng = g['meta']['n_fig']; Q = g['points'][:ng]
X = np.array([[p['x'], p['alt']] for p in Q]); C = np.array([[p['r'], p['g'], p['b']] for p in Q])
mag = (C[:, 0] > 200) & (C[:, 2] > 150) & (C[:, 1] < 130); gold = (C[:, 0] > 200) & (C[:, 1] > 150) & (C[:, 2] < 120)
green = (C[:, 1] > 180) & (C[:, 0] < 160); orange = (C[:, 0] > 200) & (C[:, 1] >= 80) & (C[:, 1] <= 150) & (C[:, 2] < 100); blue = (C[:, 2] > 200) & (C[:, 0] < 150)
seat = mag & (X[:, 1] < 58); hlot = mag & (X[:, 1] >= 58); halo = gold & (X[:, 1] > 85) & (np.abs(X[:, 0]) > 14); crown = gold & (X[:, 1] > 85) & (np.abs(X[:, 0]) <= 14)
eye = blue; saree = (green | orange) & ~eye
lhand = (X[:, 1] >= 35) & (X[:, 1] < 62) & (np.abs(X[:, 0]) > 16) & ~mag & ~saree & ~eye
lab = np.array([''] * ng, dtype=object); lab[seat] = 'seat'; lab[hlot] = 'hlotus'; lab[halo] = 'halo'; lab[crown] = 'crown'; lab[eye] = 'eye'; lab[saree] = 'saree'; lab[lhand] = 'lhand'
for p, r in zip(Q, lab): p['role'] = r
print('Lakshmi: ' + ', '.join(f'{r} {int((lab == r).sum())}' for r in ('seat', 'hlotus', 'halo', 'crown', 'eye', 'saree', 'lhand')) + f', unlabelled (still) {int((lab == "").sum())}')
g['meta']['halo_centre'] = [0.0, 96.0]
json.dump(g, open(OUT + 's12.json', 'w'))
cmap = {'seat': (1.0, 0.3, 0.8), 'hlotus': (0.4, 0.9, 1.0), 'halo': (1.0, 0.8, 0.1), 'crown': (1.0, 0.95, 0.5), 'eye': (0.3, 0.5, 1.0), 'saree': (0.4, 1.0, 0.4), 'lhand': (1.0, 0.55, 0.15), '': (0.4, 0.45, 0.6)}
fig, ax = plt.subplots(1, 2, figsize=(22, 12), facecolor='#04050b')
for a, t in zip(ax, ('Lakshmi: parts that will move', 'Mandir flag: 6 cloth drones (cyan) wave, 2 pole drones (gold) stay')): a.set_facecolor('#04050b'); a.set_title(t, color='#ffd9a0', fontsize=15); a.axis('off')
ax[0].scatter(X[:, 0], X[:, 1], s=90, c=[cmap[r] for r in lab]); ax[0].set_aspect('equal')
for r, c in cmap.items():
    if r: ax[0].scatter([], [], c=[c], label=f'{r} ({int((lab == r).sum())})')
ax[0].legend(facecolor='#0b1224', labelcolor='w', fontsize=11, loc='lower left')
M = np.array([[p['x'], p['alt']] for p in f['points'][:nf]]); mr = np.array([p['role'] for p in f['points'][:nf]])
ax[1].scatter(M[:, 0], M[:, 1], s=40, c='#3a4560'); ax[1].scatter(M[mr == 'mflag', 0], M[mr == 'mflag', 1], s=160, c='#33e0ff'); ax[1].scatter(M[mr == 'mpole', 0], M[mr == 'mpole', 1], s=160, c='#ffbf00'); ax[1].set_aspect('equal'); ax[1].set_xlim(-30, 30); ax[1].set_ylim(60, 112)
plt.tight_layout(); plt.savefig(HERE + 'lakshmi_mandir_parts.png', dpi=42, facecolor='#04050b')
