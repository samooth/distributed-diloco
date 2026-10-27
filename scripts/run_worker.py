#!/usr/bin/env python3
"""Start one P1 DiLoCo worker."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from diloco.backend import build_backend  # noqa: E402
from diloco.config import load_config  # noqa: E402
from diloco.worker import Worker  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DiLoCo P1 worker")
    parser.add_argument("--config", default="configs/smoke.json")
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--coordinator", help="e.g. http://127.0.0.1:8000")
    parser.add_argument("--discover-topic", help="Hyperswarm topic used to discover the coordinator")
    parser.add_argument("--discovery-timeout", type=float, default=30.0)
    parser.add_argument("--p2p-topic", help="Hyperswarm topic used as the coordinator transport")
    parser.add_argument("--tunnel-listen-host", default="127.0.0.1")
    parser.add_argument("--tunnel-listen-port", type=int, default=0)
    parser.add_argument("--shard-id", type=int, default=0)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--log", default="")
    parser.add_argument("--grpc-target", help="host:port for transport='grpc'")
    parser.add_argument("--tls-cert", default="")
    parser.add_argument("--tls-key", default="")
    parser.add_argument("--tls-client-ca", default="")
    parser.add_argument("--tls-verify", action="store_true", default=None)
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
    if args.coordinator and (args.discover_topic or args.p2p_topic):
        raise SystemExit("use only one of --coordinator, --discover-topic or --p2p-topic")
    if not args.coordinator and not args.discover_topic and not args.p2p_topic and not args.grpc_target:
        raise SystemExit("--coordinator, --discover-topic, --p2p-topic or --grpc-target is required")
    coordinator_url = args.coordinator
    grpc_target = args.grpc_target
    if grpc_target and not coordinator_url:
        coordinator_url = f"grpc://{grpc_target}"
    sidecar = None
    if args.discover_topic:
        from diloco.holepunch import discover_coordinator

        coordinator_url = discover_coordinator(args.discover_topic, args.discovery_timeout, args.node_id, cfg.run_id)
        logging.info("discovered coordinator: %s", coordinator_url)
    if args.p2p_topic:
        from diloco.holepunch import HolepunchSidecar

        listen_port = args.tunnel_listen_port or (cfg.coordinator.port + 10000 if cfg.coordinator.port else 18080)
        sidecar = HolepunchSidecar(
            mode="tunnel-client",
            topic=args.p2p_topic,
            name=args.node_id,
            listen_host=args.tunnel_listen_host,
            listen_port=listen_port,
        )
        sidecar.start()
        sidecar.wait_for_ready()
        coordinator_url = f"http://{args.tunnel_listen_host}:{listen_port}"
        if cfg.coordinator.transport == "grpc":
            coordinator_url = f"grpc://{args.tunnel_listen_host}:{listen_port}"
            grpc_target = f"{args.tunnel_listen_host}:{listen_port}"
        logging.info("coordinator available through Hyperswarm tunnel: %s", coordinator_url)
    if coordinator_url and coordinator_url.startswith("grpc://"):
        grpc_target = coordinator_url[len("grpc://"):]
        coordinator_url = f"http://{grpc_target}"
    if args.tls_cert:
        cfg = replace(cfg, coordinator=replace(cfg.coordinator, tls_cert=args.tls_cert))
    if args.tls_key:
        cfg = replace(cfg, coordinator=replace(cfg.coordinator, tls_key=args.tls_key))
    if args.tls_client_ca:
        cfg = replace(cfg, coordinator=replace(cfg.coordinator, tls_client_ca=args.tls_client_ca))
    if args.tls_verify is not None:
        cfg = replace(cfg, coordinator=replace(cfg.coordinator, tls_verify=args.tls_verify))
    log_path = Path(args.log) if args.log else Path(cfg.coordinator.checkpoint_dir) / "logs" / f"{args.node_id}.jsonl"
    backend = build_backend(cfg, shard_id=args.shard_id, device=args.device)
    worker = Worker(cfg, args.node_id, coordinator_url, backend, log_path=str(log_path), grpc_target=grpc_target)
    try:
        summary = worker.run()
    finally:
        if sidecar is not None:
            sidecar.stop()
        worker.close()
    print(json.dumps(summary, indent=2, sort_keys=True, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
