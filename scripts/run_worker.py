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
    parser.add_argument("--p2p-context", default="", help="bitbeam context for identity tunnels (default: run_id)")
    parser.add_argument("--coordinator-pubkey", default="", help="coordinator BSV pubkey for per-pair identity tunnel")
    parser.add_argument("--tunnel-listen-host", default="127.0.0.1")
    parser.add_argument("--tunnel-listen-port", type=int, default=0)
    parser.add_argument("--shard-id", type=int, default=0)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--log", default="")
    parser.add_argument("--grpc-target", help="host:port for transport='grpc'")
    parser.add_argument("--init-state", default="", help="local adapter bundle from export_initial_state.py; skips downloading global state when hash matches")
    parser.add_argument("--state-cache-dir", default="", help="directorio local para cache persistente de blobs de estado por hash (default: runs/state-cache)")
    parser.add_argument("--node-identity", default="", help="path to BSV identity JSON (pubkey/wif)")
    parser.add_argument("--invite", default="", help="path to coordinator join invite JSON")
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

    if args.invite:
        invite_path = Path(args.invite)
        if not invite_path.is_absolute():
            invite_path = (PROJECT_ROOT / invite_path).resolve()
        try:
            invite = json.loads(invite_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SystemExit(f"cannot read invite {invite_path}: {exc}")
        if invite.get("sig") and invite.get("signed_by"):
            try:
                from diloco.identity import canonical_invite, verify_message
                inv_copy = {k: v for k, v in invite.items() if k not in ("sig", "sig_alg", "signed_by")}
                if not verify_message(str(invite["signed_by"]), canonical_invite(inv_copy), str(invite["sig"])):
                    raise SystemExit("invite signature verification failed")
                logging.info("invite signature verified (coordinator=%s)", str(invite.get("signed_by", ""))[:16] + "...")
            except SystemExit:
                raise
            except Exception as exc:
                logging.warning("could not verify invite signature: %s", exc)
        if not args.p2p_topic and invite.get("topic"):
            args.p2p_topic = str(invite["topic"])
        if not getattr(args, "p2p_context", "") and invite.get("context"):
            args.p2p_context = str(invite["context"])
        if not args.coordinator_pubkey and invite.get("coordinator_pubkey"):
            args.coordinator_pubkey = str(invite["coordinator_pubkey"])
        if not args.coordinator and not args.discover_topic and not args.p2p_topic and not args.grpc_target and invite.get("endpoint"):
            ep = str(invite.get("endpoint", ""))
            if ep and not ep.startswith("http://0.0.0.0") and not ep.startswith("http://127.0.0.1:0"):
                args.coordinator = ep
        logging.info("loaded invite for run %s", invite.get("run_id", "?"))

    if args.coordinator and (args.discover_topic or args.p2p_topic):
        raise SystemExit("use only one of --coordinator, --discover-topic or --p2p-topic")
    if not args.coordinator and not args.discover_topic and not args.p2p_topic and not args.grpc_target:
        raise SystemExit("--coordinator, --discover-topic, --p2p-topic or --grpc-target is required")
    coordinator_url = args.coordinator
    grpc_target = args.grpc_target
    if grpc_target and not coordinator_url:
        coordinator_url = f"grpc://{grpc_target}"

    node_identity = (args.node_identity or "").strip()
    if node_identity:
        ip = Path(node_identity)
        if not ip.is_absolute():
            ip = (PROJECT_ROOT / ip).resolve()
        node_identity = str(ip)

    sidecar = None
    if args.discover_topic:
        from diloco.holepunch import discover_coordinator

        coordinator_url = discover_coordinator(args.discover_topic, args.discovery_timeout, args.node_id, cfg.run_id)
        logging.info("discovered coordinator: %s", coordinator_url)
    if args.p2p_topic:
        from diloco.holepunch import HolepunchSidecar

        listen_port = args.tunnel_listen_port or (cfg.coordinator.port + 10000 if cfg.coordinator.port else 18080)
        coord_pubkey = (args.coordinator_pubkey or "").strip()
        context = (args.p2p_context or cfg.run_id or args.p2p_topic).strip()
        tunnel_kwargs = dict(
            mode="tunnel-client",
            topic=args.p2p_topic,
            name=args.node_id,
            listen_host=args.tunnel_listen_host,
            listen_port=listen_port,
        )
        if node_identity and coord_pubkey:
            tunnel_kwargs.update(identity=node_identity, peer_pubkey=coord_pubkey, context=context)
            logging.info("using identity bitbeam tunnel (peer=%s, context=%s)", coord_pubkey[:16] + "...", context)
        sidecar = HolepunchSidecar(**tunnel_kwargs)
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
    init_state = (args.init_state or "").strip()
    if init_state in ("", "auto") and cfg.model.kind == "hf":
        candidate = (PROJECT_ROOT / "runs" / f"init-{config_path.stem}").resolve()
        if candidate.joinpath("meta.json").is_file():
            init_state = str(candidate)
            logging.info("using discovered init state: %s", init_state)
        elif init_state == "":
            init_state = ""
    if init_state and init_state != "auto":
        init_path = Path(init_state)
        if not init_path.is_absolute():
            init_path = (PROJECT_ROOT / init_path).resolve()
        init_state = str(init_path)
    state_cache_dir = (getattr(args, "state_cache_dir", "") or "").strip()
    if state_cache_dir:
        sc_path = Path(state_cache_dir)
        if not sc_path.is_absolute():
            sc_path = (PROJECT_ROOT / sc_path).resolve()
        state_cache_dir = str(sc_path)
    worker = Worker(
        cfg,
        args.node_id,
        coordinator_url,
        backend,
        log_path=str(log_path),
        grpc_target=grpc_target,
        init_state_path=(init_state and init_state != "auto") and init_state or None,
        node_identity_path=node_identity or None,
        state_cache_dir=state_cache_dir or None,
    )
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
