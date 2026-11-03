"""Independent MAVSDK client on PX4's GCS port (14550): ground truth that does not go through the GCS."""
import asyncio
import time

from mavsdk import System

T0 = time.time()


class Observer:
    def __init__(self, port=50099, url="udpin://0.0.0.0:14550"):
        self.s = System(port=port)
        self.url = url
        self.events = []
        self.alt = 0.0
        self.mode = None
        self.armed = None
        self.landed = None

    def log(self, kind, value):
        self.events.append((round(time.time() - T0, 2), kind, value))
        print(f"[{time.time() - T0:7.2f}s] OBS {kind}: {value}", flush=True)

    async def start(self):
        await self.s.connect(system_address=self.url)
        async for state in self.s.core.connection_state():
            if state.is_connected:
                break
        self.log("connected", True)
        self.tasks = [asyncio.create_task(c()) for c in (self._mode, self._armed, self._landed, self._pos)]

    async def _mode(self):
        async for mode in self.s.telemetry.flight_mode():
            mode = str(mode)
            if mode != self.mode:
                self.mode = mode
                self.log("mode", mode)

    async def _armed(self):
        async for armed in self.s.telemetry.armed():
            if armed != self.armed:
                self.armed = armed
                self.log("armed", armed)

    async def _landed(self):
        async for landed in self.s.telemetry.landed_state():
            landed = str(landed)
            if landed != self.landed:
                self.landed = landed
                self.log("landed_state", landed)

    async def _pos(self):
        async for pos in self.s.telemetry.position_velocity_ned():
            self.alt = -pos.position.down_m

    async def wait_for(self, predicate, timeout, what=""):
        start = time.time()
        while time.time() - start < timeout:
            if predicate():
                return True
            await asyncio.sleep(0.1)
        self.log("timeout", what)
        return False
