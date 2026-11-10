#!/usr/bin/env python3
"""Generate a BSV node identity (uses sidecar @bsv/sdk)."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIDECAR = PROJECT_ROOT / "network" / "holepunch-sidecar" / "identity.js"


def main() -> int:
    p = argparse.ArgumentParser(description="Generate DiLoCo node BSV identity")
    p.add_argument("--name", required=True, help="node name (e.g. coord, node-0)")
    p.add_argument("--out", default="", help="output path (default: runs/identity/<name>.json)")
    p.add_argument("--node-bin", default="node")
    args = p.parse_args()
    out = Path(args.out) if args.out else PROJECT_ROOT / "runs" / "identity" / f"{args.name}.json"
    if not out.is_absolute():
        out = (PROJECT_ROOT / out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [args.node_bin, str(SIDECAR), "--cmd", "generate", "--name", args.name, "--out", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout)
        return proc.returncode
    try:
        print(proc.stdout.strip())
    except Exception:
        print(json.dumps({"out": str(out)}))
    print(f"Identity saved: {out}", file=sys.stderr)
    print(f"Keep the WIF private. Add pubkey to coordinator allowed_node_pubkeys if needed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
