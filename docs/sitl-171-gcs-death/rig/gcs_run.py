"""Start the GCS for the #171 dead-GCS runs. LIFELINE=1 (default) or 0.

With this PR's code, LIFELINE=0 puts MAVSDK's stock System back, to show the
old behaviour on the same code. On code from before this PR there is no
GcsBoundSystem yet, and LIFELINE=1 injects it: that is how the 2026-11-02
records were taken, on main 7a6ab70 with the module from this PR.
"""
import os

import uvicorn

import tara_gcs.drone_manager as dm

lifeline = os.environ.get("LIFELINE", "1") == "1"
if hasattr(dm, "GcsBoundSystem"):
    if not lifeline:
        from mavsdk import System
        dm.GcsBoundSystem = System
elif lifeline:
    from tara_gcs.mavsdk_lifeline import GcsBoundSystem
    dm.System = GcsBoundSystem
print(f"LIFELINE {'ON' if lifeline else 'OFF'}", flush=True)
uvicorn.run("tara_gcs.main:app", host="127.0.0.1", port=8090, log_level="info")
