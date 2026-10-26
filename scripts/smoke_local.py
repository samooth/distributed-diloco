#!/usr/bin/env python3
"""Run 2+ toy workers against a localhost coordinator.

This exercises the real HTTP path, safetensors payloads, outer optimizer,
checkpointing and round state machine. It does not test a language model.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from diloco.backend import build_backend, initial_adapter_state  # noqa: E402
from diloco.checkpoint import CheckpointStore  # noqa: E402
from diloco.config import load_config  # noqa: E402
from diloco.coordinator import Coordinator  # noqa: E402
from diloco.worker import Worker  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Localhost DiLoCo smoke test")
    parser.add_argument("--config", default="configs/smoke.json")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--checkpoint-dir", default="")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be >= 1")
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    cfg = load_config(config_path, base_dir=PROJECT_ROOT)
    checkpoint_dir = (
        Path(args.checkpoint_dir).expanduser()
        if args.checkpoint_dir
        else Path(cfg.coordinator.checkpoint_dir)
    )
    if not checkpoint_dir.is_absolute():
        checkpoint_dir = (PROJECT_ROOT / checkpoint_dir).resolve()
    if args.reset and checkpoint_dir.exists():
        shutil.rmtree(checkpoint_dir)
    existing = list(checkpoint_dir.glob("checkpoint-*")) if checkpoint_dir.exists() else []
    if existing:
        raise SystemExit(
            f"{checkpoint_dir} already has checkpoints; use --reset or --checkpoint-dir"
        )
    cfg = replace(
        cfg,
        data=replace(cfg.data, num_shards=args.workers),
        coordinator=replace(
            cfg.coordinator,
            min_workers=args.workers,
            quorum=args.workers,
            max_workers=max(args.workers, 4),
            max_rounds=args.rounds,
            checkpoint_dir=str(checkpoint_dir),
        ),
    )

    state, base_hash, adapter_meta = initial_adapter_state(cfg, device=args.device)
    coordinator = Coordinator(cfg, state, base_hash, adapter_meta)
    port = coordinator.start()
    url = f"http://{cfg.coordinator.host}:{port}"
    logging.info("smoke coordinator at %s", url)

    workers: List[Worker] = []
    threads: List[threading.Thread] = []
    summaries: List[Dict[str, Any]] = []
    errors: List[str] = []
    lock = threading.Lock()

    def run_worker(index: int) -> None:
        worker = None
        try:
            backend = build_backend(cfg, shard_id=index, device=args.device)
            worker = Worker(
                cfg,
                f"node-{index}",
                url,
                backend,
                log_path=str(checkpoint_dir / "logs" / f"node-{index}.jsonl"),
            )
            with lock:
                workers.append(worker)
            summary = worker.run()
            with lock:
                summaries.append(summary)
        except Exception as exc:  # noqa: BLE001
            logging.exception("worker %s failed", index)
            with lock:
                errors.append(f"node-{index}: {exc}")
        finally:
            if worker is not None:
                worker.close()

    for index in range(args.workers):
        thread = threading.Thread(target=run_worker, args=(index,), daemon=True)
        threads.append(thread)
        thread.start()

    deadline = time.time() + max(30.0, cfg.coordinator.round_timeout_s * args.rounds + 30.0)
    for thread in threads:
        thread.join(timeout=max(0.1, deadline - time.time()))
    alive = [thread.name for thread in threads if thread.is_alive()]
    if alive:
        errors.append(f"workers still alive: {alive}")
        coordinator.stop()
        for worker in workers:
            worker.close()
        print(json.dumps({"ok": False, "errors": errors}, indent=2), flush=True)
        return 1

    status = coordinator.status()
    checkpoint = CheckpointStore(checkpoint_dir).load_latest()
    coordinator.stop()

    result = {
        "ok": not errors and status.get("failure") is None and checkpoint is not None,
        "workers": args.workers,
        "rounds": args.rounds,
        "coordinator": {
            "finished": status.get("finished"),
            "round": status.get("round"),
            "global_step": status.get("global_step"),
            "failure": status.get("failure"),
            "last_result": status.get("last_result"),
        },
        "checkpoint": {
            "path": str(checkpoint.path) if checkpoint else None,
            "round": checkpoint.round if checkpoint else None,
            "state_hash": checkpoint.meta.get("state_hash") if checkpoint else None,
        },
        "summaries": summaries,
        "errors": errors,
    }
    print(json.dumps(result, indent=2, sort_keys=True, default=str), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
