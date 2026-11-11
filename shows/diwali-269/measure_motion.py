# ruff: noqa: E401, E402, E701, E702, F811  (analysis script that records how this show was made; not product code)
import json, sys, numpy as np
import os
# usage: python measure_motion.py <folder with choreo.json, timeline.json, target_idx.json> <formations folder>
S=os.path.join(sys.argv[1],''); F=os.path.join(sys.argv[2],'')
d=json.load(open(S+'choreo.json')); tl=json.load(open(S+'timeline.json')); ti=json.load(open(S+'target_idx.json'))['targetIdx']; names=tl['names']
T=np.array([w['time'] for w in d['drones'][0]['waypoints']]); dt=0.1
X=np.array([[[w['x'],w['y'],-w['z']] for w in dr['waypoints']] for dr in d['drones']])
print(f"{'scene':26} {'movers':>6} {'max speed':>9} {'max accel':>9} {'max jerk':>8}   worst accel role")
for k,nm in enumerate(names):
    h=[s for s in tl['segments'] if s['kind']=='hold' and s['k']==k][0]; idx=np.nonzero((T>=h['t0'])&(T<=h['t1']))[0]
    Q=X[:,idx,:]; rng=np.linalg.norm(Q.max(1)-Q.min(1),axis=1); mv=np.nonzero(rng>0.02)[0]
    if len(mv)==0: print(f'{nm[:26]:26} {0:6d}'); continue
    Qm=Q[mv]; v=np.linalg.norm(np.diff(Qm,axis=1),axis=2)/dt; a=np.linalg.norm(np.diff(Qm,2,axis=1),axis=2)/dt**2; j=np.linalg.norm(-Qm[:,:-4]+2*Qm[:,1:-3]-2*Qm[:,3:-1]+Qm[:,4:],axis=2)/(2*dt**3)
    f=json.load(open(F+f's{k+1:02d}.json')); P=f['points']; roles={}
    for i in mv: kk=ti[k][i]; roles[i]=P[kk]['role'] if kk<len(P) else ''
    wi=int(np.argmax(a.max(1))); print(f'{nm[:26]:26} {len(mv):6d} {v.max():9.2f} {a.max():9.2f} {j.max():8.2f}   {roles[mv[wi]] or "(text/figure)"}  (accel {a.max(1)[wi]:.2f})')
