#!/usr/bin/env python3
"""Start the P1 HTTP coordinator."""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from diloco.backend import initial_adapter_state  # noqa: E402
from diloco.checkpoint import CheckpointStore, load_initial_state  # noqa: E402
from diloco.config import load_config  # noqa: E402
from diloco.coordinator import Coordinator  # noqa: E402
from diloco.holepunch import HolepunchSidecar  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DiLoCo P1 coordinator")
    parser.add_argument("--config", default="configs/smoke.json")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--checkpoint-dir", default="")
    parser.add_argument("--init-state", default="", help="adapter bundle from export_initial_state.py")
    parser.add_argument("--max-rounds", type=int, default=-1)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--announce-topic", help="Hyperswarm topic used to announce this coordinator")
    parser.add_argument("--sidecar-name", default="coordinator")
    parser.add_argument("--p2p-topic", help="Hyperswarm topic used to tunnel coordinator traffic")
    parser.add_argument("--p2p-target-host", default="127.0.0.1")
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
    coordinator_cfg = cfg.coordinator
    if args.checkpoint_dir:
        coordinator_cfg = replace(coordinator_cfg, checkpoint_dir=args.checkpoint_dir)
    if args.max_rounds >= 0:
        coordinator_cfg = replace(coordinator_cfg, max_rounds=args.max_rounds)
    cfg = replace(cfg, coordinator=coordinator_cfg)

    checkpoint_dir = Path(cfg.coordinator.checkpoint_dir)
    if not checkpoint_dir.is_absolute():
        checkpoint_dir = (PROJECT_ROOT / checkpoint_dir).resolve()
    if args.reset and checkpoint_dir.exists():
        shutil.rmtree(checkpoint_dir)
    store = CheckpointStore(cfg.coordinator.checkpoint_dir)
    checkpoint = None if args.no_resume else store.load_latest()
    if checkpoint is not None:
        if checkpoint.meta.get("config_hash") != cfg.fingerprint():
            raise SystemExit("checkpoint config hash does not match this config")
        base_hash = str(checkpoint.meta["base_hash"])
        coordinator = Coordinator.from_checkpoint(
            cfg,
            checkpoint,
            base_hash,
            checkpoint.meta.get("adapter_meta", {}),
        )
        logging.info("resuming from %s", checkpoint.path)
    else:
        if args.init_state:
            init_path = Path(args.init_state)
            if not init_path.is_absolute():
                init_path = (PROJECT_ROOT / init_path).resolve()
            state, base_hash, adapter_meta, init_meta = load_initial_state(init_path)
            if init_meta.get("config_hash") != cfg.fingerprint():
                raise SystemExit("initial-state config hash does not match this config")
        elif cfg.model.kind == "hf":
            raise SystemExit(
                "for kind='hf' export the initial adapter first: "
                "scripts/export_initial_state.py --config ... --out ..."
            )
        else:
            state, base_hash, adapter_meta = initial_adapter_state(cfg, device=args.device)
        coordinator = Coordinator(cfg, state, base_hash, adapter_meta)

    port = coordinator.start()
    print(f"PORT={port}", flush=True)
    print(f"COORDINATOR_URL=http://{cfg.coordinator.host}:{port}", flush=True)
    sidecars = []
    if args.announce_topic:
        sidecar = HolepunchSidecar(
            mode="announce",
            topic=args.announce_topic,
            name=args.sidecar_name,
            run_id=cfg.run_id,
            endpoint=f"http://{cfg.coordinator.host}:{port}",
        )
        sidecar.start()
        sidecars.append(sidecar)
        logging.info("announcing coordinator on Hyperswarm topic %s", args.announce_topic)
    if args.p2p_topic:
        tunnel = HolepunchSidecar(
            mode="tunnel-server",
            topic=args.p2p_topic,
            name=args.sidecar_name,
            target_host=args.p2p_target_host,
            target_port=port,
        )
        tunnel.start()
        tunnel.wait_for_ready()
        sidecars.append(tunnel)
        logging.info("coordinator available through Hyperswarm topic %s", args.p2p_topic)
    if args.no_wait:
        try:
            while True:
                import time

                time.sleep(1)
        except KeyboardInterrupt:
            for item in sidecars:
                item.stop()
            coordinator.stop()
        return 0
    try:
        status = coordinator.wait_until_finished()
    except KeyboardInterrupt:
        for item in sidecars:
            item.stop()
        coordinator.stop()
        return 130
    for item in sidecars:
        item.stop()
    print(json.dumps(status, indent=2, sort_keys=True, default=str), flush=True)
    coordinator.stop()
    return 0 if status.get("failure") is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
