#!/usr/bin/env python3
"""Exports a built Diwali show as a signed CSV through the real Studio `/api/export`.

Why CSV: the GCS refuses an upload over 5 MB, and the Diwali JSON exports are 9.4 MB at 35
drones and 45 MB at 100. CSV is the same certified flight export in a form that fits at 35
drones (about 2 MB, 73,360 rows). The GCS also caps a CSV at 100,000 rows, so 100 drones
(352,600 rows) cannot be uploaded to the GCS in any format; see docs/diwali-runbook.md.

    python tools/diwali_export_csv.py dist/diwali/diwali_35.json
    python tools/diwali_export_csv.py dist/diwali/diwali_35.json --studio http://127.0.0.1:8001

Needs the Studio backend running (`./tara up`). It is signed when the backend has a signing key
(`STUDIO_SIGNING_KEY` / `[studio].signing_key`); otherwise the CSV is written unsigned and the
script says how to sign it with `./tara sign`. A preview-only export is refused, never written.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("show", type=Path, help="exported show JSON from tools/diwali_build.mjs")
    ap.add_argument("--studio", default="http://127.0.0.1:8001")
    ap.add_argument("--rate-hz", type=float, default=10.0)
    ap.add_argument("--out", type=Path, help="CSV path (default: next to the show, .csv)")
    args = ap.parse_args()

    show = json.loads(args.show.read_text(encoding="utf-8"))
    body = {"choreo": {"drones": show["drones"]}, "format": "csv", "rate_hz": args.rate_hz,
            "home_lat": 0, "home_lon": 0, "sign": True}
    req = urllib.request.Request(args.studio.rstrip("/") + "/api/export", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            out = json.load(resp)
    except urllib.error.HTTPError as exc:
        print(f"Studio refused the export: HTTP {exc.code} {exc.read()[:400].decode(errors='replace')}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"cannot reach Studio at {args.studio}: {exc.reason} (is `./tara up` running?)", file=sys.stderr)
        return 1
    if out.get("preview_only"):
        print("Studio returned a preview-only export, which cannot be flown; not writing it.", file=sys.stderr)
        return 1

    csv_path = args.out or args.show.with_suffix(".csv")
    csv_path.write_text(out["content"], encoding="utf-8")
    rows = out["content"].count("\n") - 1
    print(f"wrote {csv_path} ({len(out['content']) / 1e6:.2f} MB, {rows} rows)")
    if rows > 100_000 or len(out["content"]) > 5 * 1024 * 1024:
        print("WARNING: over the GCS upload limits (5 MB, 100,000 rows); the GCS will refuse this file.", file=sys.stderr)
    sig = out.get("signature")
    if sig:
        sig_path = Path(f"{csv_path}.sig.json")
        sig_path.write_text(sig["content"], encoding="utf-8")
        print(f"wrote {sig_path} (key {sig.get('key_id')})")
    else:
        print(f"unsigned: sign it with  ./tara sign {csv_path} --key <your-key.pem>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
