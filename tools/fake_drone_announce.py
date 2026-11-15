#!/usr/bin/env python3
"""A fake drone that announces itself to a GCS fleet-discovery listener (issue #58).

    python tools/fake_drone_announce.py --key-hex <hex> --uuid 0xA1B2C3D4E5F60718 [--host 127.0.0.1] [--port 14590] [--rate 2]

Sends one authenticated announcement frame (see tara_gcs/fleet_registry.py) at ``--rate`` Hz from an
ordinary ephemeral UDP port, until it is stopped. Prints ``FIRST_SENT <epoch seconds>`` after the first
frame so a test can measure how quickly the GCS noticed. Use it to try discovery by hand, or as the
"start a drone on an arbitrary UDP port / kill it / power it back on" part of the acceptance test.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

try:
    from tara_gcs.fleet_registry import encode_announcement
except ModuleNotFoundError:  # run from a checkout without the package installed
    root = Path(__file__).resolve().parent.parent / "packages"
    sys.path[:0] = [str(root / "tara-gcs"), str(root / "tara-core")]
    from tara_gcs.fleet_registry import encode_announcement


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key-hex", required=True, help="the discovery key, hex (at least 32 hex characters)")
    ap.add_argument("--uuid", required=True, help="64-bit hardware id, e.g. 0xA1B2C3D4E5F60718")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=14590)
    ap.add_argument("--rate", type=float, default=2.0, help="announcements per second (default 2)")
    args = ap.parse_args(argv)
    try:
        key, uuid = bytes.fromhex(args.key_hex), int(args.uuid, 0)
        encode_announcement(key, uuid)
    except ValueError as exc:
        print(f"fake_drone_announce: {exc}", file=sys.stderr)
        return 1
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # no bind: the OS picks the source port
    interval = 1.0 / args.rate
    first = True
    while True:
        sock.sendto(encode_announcement(key, uuid), (args.host, args.port))
        if first:
            print(f"FIRST_SENT {time.time():.6f}", flush=True)
            first = False
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
