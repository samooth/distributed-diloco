#!/usr/bin/env python3
"""Export the initial adapter state without starting a run.

Run this on a machine that can load the base model. The coordinator then loads
only the small adapter bundle, which matters for 32B/70B.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from diloco.backend import initial_adapter_state  # noqa: E402
from diloco.checkpoint import load_initial_state, save_initial_state  # noqa: E402
from diloco.config import load_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a coordinator initial adapter bundle")
    parser.add_argument("--config", default="configs/smoke.json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    cfg = load_config(config_path, base_dir=PROJECT_ROOT)
    out = Path(args.out)
    if not out.is_absolute():
        out = (PROJECT_ROOT / out).resolve()
    state, base_hash, adapter_meta = initial_adapter_state(cfg, device=args.device)
    save_initial_state(out, state, base_hash, adapter_meta, cfg.fingerprint())
    loaded, loaded_base, loaded_meta, meta = load_initial_state(out)
    print(
        json.dumps(
            {
                "path": str(out),
                "state_hash": meta["state_hash"],
                "base_hash": loaded_base,
                "adapter_meta": loaded_meta,
                "state_bytes": sum(v.numel() * v.element_size() for v in loaded.values()),
            },
            indent=2,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
