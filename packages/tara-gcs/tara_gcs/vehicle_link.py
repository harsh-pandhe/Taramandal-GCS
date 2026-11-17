"""The MAVSDK system address a px4-backend drone is connected on (issue #67).

Until now every drone was reached on ``udpin://127.0.0.1:<port>`` (PX4 SITL). A real autopilot is reached on a serial
port, ``serial:///dev/ttyACM0:921600`` (MAVSDK's own form). This module is the one place an address is accepted: an
address is validated and refused, never guessed, because it decides which vehicle the GCS will send flight commands to.

Nothing here changes the operating mode or opens a hardware gate: ``HARDWARE`` mode, the hardware certification flag and
every command gate are untouched, and a serial address in ``SIMULATION`` mode is exactly as gated as a UDP one. A serial
link is a transport, not a permission.
"""

from __future__ import annotations

import re

#: Baud rates MAVSDK's serial connection and common autopilots use. An unlisted rate is refused, not rounded.
BAUD_RATES = frozenset({9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600, 1500000, 2000000})

_UDP = re.compile(r"^(udpin|udpout)://([A-Za-z0-9_.\-]+):(\d{1,5})$")
_SERIAL = re.compile(r"^serial://(/[A-Za-z0-9_.\-/]+):(\d{1,8})$")


def udp_address(port: int) -> str:
    """The address a drone has always been reached on: PX4 SITL's onboard link on this machine."""
    return f"udpin://127.0.0.1:{port}"


def validate_address(address: str) -> str:
    """The address, unchanged, if it is one this GCS will connect to; ``ValueError`` saying why if not.

    Accepted: ``udpin://host:port`` / ``udpout://host:port`` (port 1-65535) and ``serial:///absolute/path:baud`` (baud one
    of ``BAUD_RATES``, always given: no default is assumed). Anything else, including other schemes MAVSDK knows, a serial
    path without a baud rate, a relative path or whitespace, is refused.
    """
    if not isinstance(address, str) or not address.strip() or address != address.strip():
        raise ValueError(f"vehicle address {address!r} is empty or has surrounding whitespace")
    m = _UDP.match(address)
    if m:
        if not 1 <= int(m.group(3)) <= 65535:
            raise ValueError(f"vehicle address {address!r}: port must be 1-65535")
        return address
    m = _SERIAL.match(address)
    if m:
        if ".." in m.group(1).split("/"):
            raise ValueError(f"vehicle address {address!r}: the device path must not contain '..'")
        if int(m.group(2)) not in BAUD_RATES:
            raise ValueError(f"vehicle address {address!r}: baud rate must be one of {sorted(BAUD_RATES)}")
        return address
    raise ValueError(
        f"vehicle address {address!r} is not one this GCS connects to: expected udpin://host:port, udpout://host:port "
        "or serial:///absolute/device/path:baud"
    )


def parse_vehicle_addresses(text: str | None, fleet_size: int) -> dict[int, str]:
    """``GCS_VEHICLE_ADDRESSES``: ``"0=serial:///dev/ttyACM0:921600,1=udpin://127.0.0.1:14541"``.

    An empty or missing value is no override (every drone keeps its UDP default). Every entry must name its drone
    (``index=address``), every index must be inside the fleet and appear once, and every address must validate.

    Raises:
        ValueError: On any bad entry. The caller must not start with a partial or guessed map.
    """
    if text is None or not text.strip():
        return {}
    out: dict[int, str] = {}
    for entry in text.split(","):
        index_text, sep, address = entry.strip().partition("=")
        if not sep or not index_text.strip().isdigit():
            raise ValueError(f"vehicle address entry {entry.strip()!r} must look like 0=serial:///dev/ttyACM0:921600")
        index = int(index_text)
        if not 0 <= index < fleet_size:
            raise ValueError(f"vehicle address entry {entry.strip()!r}: drone {index} is outside the fleet (0..{fleet_size - 1})")
        if index in out:
            raise ValueError(f"vehicle address for drone {index} is given twice")
        out[index] = validate_address(address.strip())
    return out


def serial_device(address: str | None) -> str | None:
    """The device path of a ``serial://`` address, or ``None`` for any other (or no) address."""
    m = _SERIAL.match(address or "")
    return m.group(1) if m else None
