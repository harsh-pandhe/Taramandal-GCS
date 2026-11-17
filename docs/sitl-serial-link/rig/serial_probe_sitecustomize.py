# ruff: noqa
"""Probe for the #67 serial verification (untracked, removed afterwards). Loaded only by the GCS process: timestamps every
position/landed item and times every MAVSDK flight command at the call, writing to $CADENCE_LOG. No GCS code is modified."""
import json
import os
import time

try:
    cmdline = open("/proc/self/cmdline", "rb").read().decode("utf-8", "replace")
except OSError:
    cmdline = ""

if "tara_gcs.main:app" in cmdline:
    import mavsdk.action as A
    import mavsdk.offboard as O
    import mavsdk.telemetry as T

    LOG = open(os.environ.get("CADENCE_LOG", "/tmp/cadence.jsonl"), "a", buffering=1)

    def wrap_stream(name):
        orig = getattr(T.Telemetry, name)

        async def gen(self, *a, **k):
            async for item in orig(self, *a, **k):
                LOG.write(json.dumps({"s": name, "t": time.time()}) + "\n")
                yield item

        setattr(T.Telemetry, name, gen)

    for n in ("landed_state", "position_velocity_ned"):
        wrap_stream(n)

    def wrap_cmd(cls, label, name):
        orig = getattr(cls, name)

        async def call(self, *a, **k):
            t0 = time.time()
            ok = True
            try:
                return await orig(self, *a, **k)
            except Exception:
                ok = False
                raise
            finally:
                LOG.write(json.dumps({"s": f"cmd:{label}.{name}", "t": t0, "dt": time.time() - t0, "ok": ok}) + "\n")

        setattr(cls, name, call)

    for n in ("arm", "disarm", "land", "return_to_launch", "hold", "kill", "takeoff"):
        wrap_cmd(A.Action, "action", n)
    for n in ("start", "stop"):
        wrap_cmd(O.Offboard, "offboard", n)
