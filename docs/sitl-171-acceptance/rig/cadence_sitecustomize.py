"""Cadence logging for the #171 acceptance. Loaded only by the GCS process (matched on its
command line); it wraps two MAVSDK telemetry generators to timestamp every item as it arrives
on the GCS's own connection and calls through unchanged. No GCS source file is modified."""

import json
import os
import time

try:
    cmdline = open("/proc/self/cmdline", "rb").read().decode("utf-8", "replace")
except OSError:
    cmdline = ""

if "tara_gcs.main:app" in cmdline:
    import mavsdk.telemetry as T

    LOG = open(
        os.environ.get("CADENCE_LOG", "/scratch/cadence.jsonl"), "a", buffering=1
    )

    def wrap(name):
        orig = getattr(T.Telemetry, name)

        async def gen(self, *a, **k):
            async for item in orig(self, *a, **k):
                LOG.write(
                    json.dumps(
                        {
                            "s": name,
                            "t": time.time(),
                            "v": str(item) if name == "landed_state" else None,
                        }
                    )
                    + "\n"
                )
                yield item

        setattr(T.Telemetry, name, gen)

    for n in ("landed_state", "position_velocity_ned"):
        wrap(n)
