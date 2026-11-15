"""The opt-in UDP listener and watchdog task that feed a ``FleetRegistry`` (issue #58).

Off unless the operator turns it on (``GCS_DISCOVERY=1``), and it refuses to start without a key:
it would be an unauthenticated way to put drones into the fleet. It listens for announcement frames
(see ``fleet_registry``), hands each datagram to the registry untouched, and sweeps for drones that
have gone quiet on a short timer. It never raises out of the receive path (a bad packet is counted
and dropped), never touches ``DroneManager`` and never decides anything about flight.

Binding. The default host is loopback; to receive announcements from a subnet set
``GCS_DISCOVERY_HOST`` (for example ``0.0.0.0``). Every accepted frame carries a valid HMAC tag, so
the exposure of a wider bind is the cost of parsing and counting packets that fail it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from dataclasses import asdict

from tara_gcs.fleet_registry import KEY_MIN_BYTES, FleetRegistry, Status, Transition

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 14590
SWEEP_PERIOD_S = 0.1
RECENT_EVENTS = 100


class _Listener(asyncio.DatagramProtocol):
    def __init__(self, service: "FleetDiscovery"):
        self.service = service
        self.closed: asyncio.Future = asyncio.get_running_loop().create_future()

    def connection_lost(self, exc) -> None:
        if not self.closed.done():
            self.closed.set_result(None)

    def datagram_received(self, data: bytes, addr) -> None:
        self.service.handle(data)

    def error_received(self, exc: Exception) -> None:  # e.g. an ICMP error: nothing to do but note it
        logger.debug("Fleet discovery: socket error %s", exc)


class FleetDiscovery:
    def __init__(self, registry: FleetRegistry, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 sweep_period_s: float = SWEEP_PERIOD_S):
        self.registry = registry
        self.host = host
        self.port = port
        self.sweep_period_s = sweep_period_s
        self.events: deque[dict] = deque(maxlen=RECENT_EVENTS)
        self._transport: asyncio.DatagramTransport | None = None
        self._listener: _Listener | None = None
        self._task: asyncio.Task | None = None

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> int:
        """Bind and start the watchdog. Returns the port actually bound (useful when ``port`` is 0)."""
        loop = asyncio.get_running_loop()
        self._transport, self._listener = await loop.create_datagram_endpoint(
            lambda: _Listener(self), local_addr=(self.host, self.port)
        )
        self.port = self._transport.get_extra_info("sockname")[1]
        self._task = asyncio.create_task(self._watchdog(), name="fleet-discovery-watchdog")
        logger.info("Fleet discovery listening on %s:%d (stale after %.1f s)", self.host, self.port, self.registry.stale_after_s)
        return self.port

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._transport is not None:
            self._transport.close()
            await self._listener.closed  # the socket is really closed, so the port is free when this returns
            self._transport = self._listener = None

    # ------------------------------------------------------------------- work

    def handle(self, data: bytes) -> None:
        """One datagram. Never raises."""
        try:
            result = self.registry.observe(data)
        except Exception:
            logger.exception("Fleet discovery: dropping a packet that broke the registry")
            return
        if result.status is Status.OK and (result.joined or result.recovered):
            self._record(Transition("joined" if result.joined else "recovered", result.entry.hardware_uuid,
                                    result.entry.drone_id, self.registry.wall_clock()))

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_period_s)
            try:
                for transition in self.registry.sweep():
                    self._record(transition)
            except Exception:  # the watchdog must outlive anything one sweep trips over
                logger.exception("Fleet discovery: sweep failed")

    def _record(self, t: Transition) -> None:
        self.events.append({**asdict(t), "hardware_uuid": f"{t.hardware_uuid:016x}"})

    def status(self) -> dict:
        return {"enabled": True, "host": self.host, "port": self.port, **self.registry.snapshot(),
                "events": list(self.events)}


def load_key(environ=os.environ) -> bytes | None:
    """The discovery key from ``GCS_DISCOVERY_KEY_FILE`` (a file holding hex) or ``GCS_DISCOVERY_KEY``
    (hex). None when neither is set; raises ValueError when one is set but unusable."""
    path = environ.get("GCS_DISCOVERY_KEY_FILE")
    text = None
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read().strip()
        except OSError as exc:
            raise ValueError(f"cannot read GCS_DISCOVERY_KEY_FILE: {exc}") from exc
    elif environ.get("GCS_DISCOVERY_KEY"):
        text = environ["GCS_DISCOVERY_KEY"].strip()
    if text is None:
        return None
    try:
        key = bytes.fromhex(text)
    except ValueError as exc:
        raise ValueError("the discovery key must be hex") from exc
    if len(key) < KEY_MIN_BYTES:
        raise ValueError(f"the discovery key must be at least {KEY_MIN_BYTES} bytes ({KEY_MIN_BYTES * 2} hex characters)")
    return key


def build_from_env(environ=os.environ, store_dir: str | None = None) -> FleetDiscovery | None:
    """The service for the GCS startup, or None when discovery is off or cannot be started safely."""
    if environ.get("GCS_DISCOVERY", "0") != "1":
        return None
    try:
        key = load_key(environ)
        if key is None:
            raise ValueError("set GCS_DISCOVERY_KEY_FILE (or GCS_DISCOVERY_KEY): discovery will not run unauthenticated")
        store = environ.get("GCS_DISCOVERY_STORE") or (os.path.join(store_dir, "fleet_registry.json") if store_dir else None)
        registry = FleetRegistry(
            key=key,
            store_path=store,
            stale_after_s=float(environ.get("GCS_DISCOVERY_STALE_S", 1.5)),
        )
        return FleetDiscovery(registry, host=environ.get("GCS_DISCOVERY_HOST", DEFAULT_HOST),
                              port=int(environ.get("GCS_DISCOVERY_PORT", DEFAULT_PORT)))
    except (ValueError, OSError):
        logger.exception("Fleet discovery is enabled but its settings are invalid; starting the GCS without it")
        return None
