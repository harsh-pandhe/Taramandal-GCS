# T2 (PX4 SITL + Gazebo): one row per run

Every run record under `docs/sitl-planned-vs-actual/` (65 records, including the historical runs that failed and the runs before the fixes
that made them pass), tabulated by the repo's own `tools/sitl_planned_vs_actual.py summarize`. Each record's JSON holds the commit it was flown on
(`commit`), the Studio-planned values (`planned`: duration, minimum separation, peak velocity, jerk, peak altitude) and the Gazebo-observed values
(`actual`); this table shows the columns the tool prints. It is regenerated, not hand-edited:

```
python tools/sitl_planned_vs_actual.py summarize $(find docs/sitl-planned-vs-actual -name "*.json" | sort)
```

Tier: T2 (PX4 SITL + Gazebo), simulation only, headless unless a run's folder says otherwise. Not hardware. Read the per-folder READMEs
(`docs/sitl-planned-vs-actual.md` and the folders next to it) for what each group of runs was for; a FAIL row is a kept record, not a typo.

| Run | Drones | Verdict | Planned duration (s) | Actual playback (s) | Studio min sep (m) | Gazebo min sep (m) | Max tracking error (m) | Start transient (s) | Max error after transient (m) | RTF | Abort |
|---|---|---|---|---|---|---|---|---|---|---|---|
| asis-n3-p1 | 3 | FAIL: Gazebo min separation 0.562 m < 1.5 m | 90.2 | 90.3 | 2.05 | 0.562 | 8.497 (drone 2 at 0.0 s) | never (drones 1, 2) | 8.493 | 0.999 | none |
| asis-n3-p2 | 3 | FAIL: ended in RTL; emergency transition; Gazebo min separation 0.336 m < 1.5 m; final state RTL | 90.2 |  | 2.05 | 0.336 | 22.852 (drone 0 at 71.9 s) | never (drones 1, 2) | 22.851 | 0.919 | failsafe:geofence, failsafe:proximity, failsafe:geofence |
| asis-n3-p3 | 3 | FAIL: Gazebo min separation 0.46 m < 1.5 m | 90.2 | 90.23 | 2.05 | 0.46 | 8.586 (drone 2 at 0.0 s) | never (drones 1, 2) | 8.585 | 0.998 | none |
| baseline-f3dive-n3-p1 | 3 | PASS | 84.2 | 84.32 | 2.2 | 2.169 | 5.956 (drone 2 at 0.1 s) | 3.71 | 0.496 | 0.993 | none |
| baseline-f3dive-n3-p2 | 3 | PASS | 84.2 | 84.26 | 2.2 | 2.159 | 5.979 (drone 2 at 0.0 s) | 3.64 | 0.497 | 0.997 | none |
| baseline-f3dive-n3-p3 | 3 | PASS | 84.2 | 84.28 | 2.2 | 2.147 | 5.911 (drone 2 at 0.0 s) | 3.52 | 0.5 | 1.0 | none |
| baseline-f3dive-n5-p1 | 5 | PASS | 84.8 | 84.86 | 2.2 | 2.152 | 9.928 (drone 4 at 0.0 s) | 5.52 | 0.497 | 0.98 | none |
| baseline-f3dive-n8-p1 | 8 | PASS | 87.4 | 87.44 | 2.2 | 2.106 | 15.911 (drone 7 at 0.1 s) | 7.67 | 0.643 | 0.913 | none |
| d3-n5-r01 | ? | INVALID: VM swapped during stack start (pswpout +104151 pages) |  |  |  |  |  (drone  at  s) |  |  |  | none |
| d3-n5-r02 | ? | CLEAN |  |  |  |  |  (drone  at  s) |  |  |  | none |
| d3-n5-r02 | 5 | PASS | 84.8 | 84.86 | 2.2 | 2.159 | 0.459 (drone 4 at 6.8 s) | 0.02 | 0.459 | 0.933 | none |
| d3-n5-r03 | ? | CLEAN |  |  |  |  |  (drone  at  s) |  |  |  | none |
| d3-n5-r03 | 5 | PASS | 84.8 | 84.8 | 2.2 | 2.13 | 0.422 (drone 4 at 7.1 s) | 0.0 | 0.422 | 0.917 | none |
| d3-n5-r04 | ? | CLEAN |  |  |  |  |  (drone  at  s) |  |  |  | none |
| d3-n5-r04 | 5 | PASS | 84.8 | 84.9 | 2.2 | 2.104 | 0.509 (drone 4 at 7.2 s) | 0.01 | 0.509 | 0.913 | none |
| d3-n5-r05 | ? | CLEAN |  |  |  |  |  (drone  at  s) |  |  |  | none |
| d3-n5-r05 | 5 | PASS | 84.8 | 84.88 | 2.2 | 2.102 | 0.452 (drone 3 at 92.3 s) | 0.02 | 0.452 | 0.922 | none |
| d3-n5-r06 | ? | CLEAN |  |  |  |  |  (drone  at  s) |  |  |  | none |
| d3-n5-r06 | 5 | PASS | 84.8 | 84.94 | 2.2 | 2.124 | 0.479 (drone 4 at 7.9 s) | 0.02 | 0.479 | 0.92 | none |
| d3-n5-r07 | ? | CLEAN |  |  |  |  |  (drone  at  s) |  |  |  | none |
| d3-n5-r07 | 5 | PASS | 84.8 | 84.83 | 2.2 | 2.126 | 0.433 (drone 1 at 21.8 s) | 0.01 | 0.433 | 0.926 | none |
| d3-n5-r08 | ? | CLEAN |  |  |  |  |  (drone  at  s) |  |  |  | none |
| d3-n5-r08 | 5 | PASS | 84.8 | 84.86 | 2.2 | 2.105 | 0.474 (drone 4 at 7.5 s) | 0.01 | 0.474 | 0.914 | none |
| d3-n5-r09 | ? | CLEAN |  |  |  |  |  (drone  at  s) |  |  |  | none |
| d3-n5-r09 | 5 | PASS | 84.8 | 84.89 | 2.2 | 2.138 | 0.509 (drone 4 at 7.2 s) | 0.0 | 0.509 | 0.906 | none |
| d3-n5-r10 | ? | CLEAN |  |  |  |  |  (drone  at  s) |  |  |  | none |
| d3-n5-r10 | 5 | PASS | 84.8 | 84.92 | 2.2 | 2.122 | 0.487 (drone 4 at 7.1 s) | 0.0 | 0.487 | 0.908 | none |
| d3-n5-r11 | ? | CLEAN |  |  |  |  |  (drone  at  s) |  |  |  | none |
| d3-n5-r11 | 5 | PASS | 84.8 | 84.9 | 2.2 | 2.161 | 0.442 (drone 1 at 92.5 s) | 0.01 | 0.442 | 0.905 | none |
| enu-n10-p1 | 10 | PASS (degraded: real-time factor 0.648) | 115.4 | 115.41 | 2.05 | 1.974 | 19.937 (drone 9 at 0.0 s) | 9.83 | 3.452 | 0.648 | none |
| enu-n3-p1 | 3 | PASS | 90.2 | 90.28 | 2.05 | 2.022 | 5.899 (drone 2 at 0.1 s) | 3.69 | 0.456 | 0.996 | none |
| enu-n3-p2 | 3 | PASS | 90.2 | 90.28 | 2.05 | 2.097 | 5.945 (drone 2 at 0.1 s) | 3.79 | 0.49 | 0.998 | none |
| enu-n3-p3 | 3 | PASS | 90.2 | 90.3 | 2.05 | 2.06 | 5.862 (drone 2 at 0.1 s) | 3.53 | 0.498 | 0.999 | none |
| enu-n5-p1 | 5 | PASS | 101.4 | 101.41 | 2.05 | 2.008 | 9.956 (drone 4 at 0.0 s) | 5.62 | 0.483 | 0.958 | none |
| enu-n7-p1 | 7 | PASS | 106.6 | 106.65 | 2.05 | 2.023 | 13.985 (drone 6 at 0.0 s) | 7.29 | 0.491 | 0.966 | none |
| enu-n8-p1 | 8 | PASS | 110.4 | 110.49 | 2.05 | 2.036 | 15.93 (drone 7 at 0.1 s) | 7.56 | 0.499 | 0.956 | none |
| enu-n9-p1 | 9 | PASS (degraded: real-time factor 0.841) | 107 | 107.03 | 2.05 | 2.006 | 17.877 (drone 8 at 0.0 s) | 8.93 | 0.865 | 0.841 | none |
| f3-post-n3-p1 | 3 | PASS | 84.2 | 84.27 | 2.2 | 2.14 | 0.29 (drone 1 at 17.5 s) | 0.01 | 0.29 | 0.999 | none |
| f3-post-n3-p2 | 3 | PASS | 84.2 | 84.32 | 2.2 | 2.096 | 0.306 (drone 2 at 5.0 s) | 0.01 | 0.306 | 0.999 | none |
| f3-post-n3-p3 | 3 | PASS | 84.2 | 84.27 | 2.2 | 2.181 | 0.286 (drone 1 at 86.4 s) | 0.01 | 0.286 | 0.999 | none |
| final-n3-p1-retry1 | 3 | PASS | 84.2 | 84.24 | 2.2 | 2.122 | 0.332 (drone 2 at 80.0 s) | 0.02 | 0.332 | 0.999 | none |
| final-n3-p2 | 3 | PASS | 84.2 | 84.21 | 2.2 | 2.114 | 0.519 (drone 0 at 53.9 s) | 0.02 | 0.519 | 1.0 | none |
| final-n3-p3 | 3 | PASS | 84.2 | 84.19 | 2.2 | 2.154 | 0.274 (drone 1 at 23.4 s) | 0.02 | 0.274 | 1.0 | none |
| final-n5-p1 | 5 | FAIL: emergency transition; final state RTL | 85.4 | 85.5 | 2.2 | 2.142 | 11.97 (drone 1 at 64.7 s) | 0.0 | 11.97 | 0.575 | failsafe:geofence, failsafe:geofence, failsafe:geofence |
| final-n5-p2 | 5 | PASS | 85.4 | 85.47 | 2.2 | 2.15 | 0.326 (drone 2 at 28.6 s) | 0.01 | 0.326 | 0.998 | none |
| final-n5-p3 | 5 | PASS | 85.4 | 85.39 | 2.2 | 2.137 | 0.276 (drone 1 at 28.6 s) | 0.0 | 0.276 | 1.0 | none |
| final-n5-p4 | 5 | PASS | 85.4 | 85.43 | 2.2 | 2.134 | 0.292 (drone 4 at 6.9 s) | 0.01 | 0.292 | 1.0 | none |
| f5-1 | 5 | PASS | 102.7 | 102.79 | 2.05 | 2.096 | 0.448 (drone 4 at 40.0 s) | 0.02 | 0.448 | 0.977 | none |
| f5-2 | 5 | PASS | 102.7 | 102.8 | 2.05 | 1.997 | 0.345 (drone 4 at 40.1 s) | 0.01 | 0.345 | 0.994 | none |
| f5-3 | 5 | PASS | 102.7 | 102.7 | 2.05 | 1.986 | 0.421 (drone 4 at 40.1 s) | 0.01 | 0.421 | 0.978 | none |
| f5-4 | 5 | PASS | 102.7 | 102.77 | 2.05 | 1.989 | 0.42 (drone 4 at 40.1 s) | 0.02 | 0.42 | 0.986 | none |
| f5-5 | 5 | PASS | 102.7 | 102.72 | 2.05 | 2.063 | 0.436 (drone 4 at 40.0 s) | 0.0 | 0.436 | 0.974 | none |
| f5-6 | 5 | PASS | 102.7 | 102.75 | 2.05 | 2.038 | 0.405 (drone 4 at 83.7 s) | 0.02 | 0.405 | 0.975 | none |
| s10-1 | 10 | PASS | 120.2 | 120.25 | 2.05 | 2.019 | 0.391 (drone 9 at 55.8 s) | 0.02 | 0.391 | 0.999 | none |
| s10-2 | 10 | PASS | 120.2 | 120.25 | 2.05 | 2.025 | 0.396 (drone 8 at 56.0 s) | 0.01 | 0.396 | 0.999 | none |
| s10-3 | 10 | PASS | 120.2 | 120.29 | 2.05 | 1.995 | 0.914 (drone 9 at 10.6 s) | 0.01 | 0.914 | 0.984 | none |
| postf3-n3-p1 | 3 | PASS | 84.2 | 84.25 | 2.2 | 2.119 | 0.279 (drone 1 at 15.3 s) | 0.0 | 0.279 | 0.999 | none |
| postf3-n3-p2 | 3 | PASS | 84.2 | 84.28 | 2.2 | 2.138 | 0.308 (drone 1 at 15.2 s) | 0.0 | 0.308 | 0.999 | none |
| postf3-n3-p3 | 3 | PASS | 84.2 | 84.28 | 2.2 | 2.167 | 0.322 (drone 1 at 23.4 s) | 0.01 | 0.322 | 0.999 | none |
| postf3-n5-p1 | 5 | PASS (degraded: real-time factor 0.885) | 84.8 | 84.9 | 2.2 | 2.123 | 0.532 (drone 4 at 7.2 s) | 0.0 | 0.532 | 0.885 | none |
| postf3-n8-p1 | 8 | PASS | 87.4 | 87.49 | 2.2 | 2.113 | 0.533 (drone 7 at 11.6 s) | 0.01 | 0.533 | 0.934 | none |
