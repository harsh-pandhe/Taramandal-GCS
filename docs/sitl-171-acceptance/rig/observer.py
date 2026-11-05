import asyncio
import time

from mavsdk import System

T0 = time.time()


class Observer:
    """Independent MAVSDK client on PX4's GCS port (14550): ground truth that does not go through the GCS."""

    def __init__(self, port=50099, url="udpin://0.0.0.0:14550"):
        self.s = System(port=port)
        self.url = url
        self.events = []
        self.samples = []  # (t, alt_m, vel_down_m_s, mode, armed)
        self.alt = 0.0
        self.vd = 0.0
        self.mode = None
        self.armed = None
        self.landed = None

    def log(self, kind, val):
        self.events.append((round(time.time() - T0, 2), kind, val))
        print(f"[{time.time() - T0:7.2f}s] OBS {kind}: {val}", flush=True)

    async def start(self):
        await self.s.connect(system_address=self.url)
        async for st in self.s.core.connection_state():
            if st.is_connected:
                break
        self.log("connected", True)
        self.tasks = [
            asyncio.create_task(c())
            for c in (self._mode, self._armed, self._landed, self._pos)
        ]

    async def _mode(self):
        async for m in self.s.telemetry.flight_mode():
            m = str(m)
            if m != self.mode:
                self.mode = m
                self.log("mode", m)

    async def _armed(self):
        async for a in self.s.telemetry.armed():
            if a != self.armed:
                self.armed = a
                self.log("armed", a)

    async def _landed(self):
        async for v in self.s.telemetry.landed_state():
            v = str(v)
            if v != self.landed:
                self.landed = v
                self.log("landed_state", v)

    async def _pos(self):
        last = 0.0
        async for p in self.s.telemetry.position_velocity_ned():
            self.alt = -p.position.down_m
            self.vd = p.velocity.down_m_s
            now = time.time()
            if now - last >= 0.2:  # 5 Hz is plenty for the record
                last = now
                self.samples.append(
                    (
                        round(now - T0, 2),
                        round(self.alt, 3),
                        round(self.vd, 3),
                        self.mode,
                        self.armed,
                    )
                )

    async def wait_for(self, pred, timeout, what=""):
        t = time.time()
        while time.time() - t < timeout:
            if pred():
                return True
            await asyncio.sleep(0.1)
        self.log("timeout", what)
        return False
