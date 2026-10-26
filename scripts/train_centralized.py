#!/usr/bin/env python3
"""Train the centralized LoRA baseline used to measure the DiLoCo loss gap."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from diloco.baseline import default_eval_every, default_steps, train_centralized  # noqa: E402
from diloco.config import load_config  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Centralized LoRA baseline")
    parser.add_argument("--config", default="configs/smoke.json")
    parser.add_argument("--steps", type=int, default=0)
    parser.add_argument("--eval-every", type=int, default=0)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--out", default="")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    cfg = load_config(config_path, base_dir=PROJECT_ROOT)
    steps = args.steps if args.steps > 0 else default_steps(cfg)
    eval_every = args.eval_every if args.eval_every > 0 else default_eval_every(cfg)
    out_dir = args.out
    if not out_dir:
        out_dir = str(Path(cfg.coordinator.checkpoint_dir).parent / f"centralized-{cfg.run_id}")
    out = Path(out_dir)
    if not out.is_absolute():
        out = (PROJECT_ROOT / out).resolve()
    result = train_centralized(
        cfg,
        steps=steps,
        eval_every=eval_every,
        device=args.device,
        out_dir=out,
    )
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
