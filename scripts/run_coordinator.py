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
from diloco.checkpoint import CheckpointStore, load_initial_state, save_initial_state  # noqa: E402
from diloco.config import load_config  # noqa: E402
from diloco.coordinator import Coordinator  # noqa: E402
from diloco.holepunch import HolepunchSidecar  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DiLoCo P1 coordinator")
    parser.add_argument("--config", default="configs/smoke.json")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--checkpoint-dir", default="")
    parser.add_argument("--init-state", default="", help="adapter bundle from export_initial_state.py, or 'auto' to use/export runs/init-<config>")
    parser.add_argument("--max-rounds", type=int, default=-1)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--announce-topic", help="Hyperswarm topic used to announce this coordinator")
    parser.add_argument("--sidecar-name", default="coordinator")
    parser.add_argument("--p2p-topic", help="Hyperswarm topic used to tunnel coordinator traffic")
    parser.add_argument("--p2p-target-host", default="127.0.0.1")
    parser.add_argument("--p2p-context", default="", help="bitbeam context for identity tunnels (default: run_id)")
    parser.add_argument("--coordinator-identity", default="", help="path to coordinator BSV identity JSON; enables per-pair bitbeam tunnels when used with --p2p-topic")
    parser.add_argument("--tls-cert", default="")
    parser.add_argument("--tls-key", default="")
    parser.add_argument("--tls-client-ca", default="")
    parser.add_argument("--tls-require-client-cert", action="store_true")
    parser.add_argument("--allow-pubkey", action="append", default=[], help="allowed worker BSV pubkey (repeatable)")
    parser.add_argument("--allow-pubkey-file", action="append", default=[], help="file with allowed pubkeys (one per line or JSON list)")
    parser.add_argument("--require-node-identity", action="store_true")
    parser.add_argument("--print-invite", default="", help="write a signed join invite JSON to this path and exit after startup info is ready")
    parser.add_argument("--log-level", default="INFO")
    return parser


def _build_invite(cfg, coordinator, port: int, args, coord_identity: str, p2p_context: str) -> dict:
    import time as _time
    from diloco.identity import canonical_invite, get_pubkey, sign_message

    coord_pubkey = ""
    if coord_identity:
        try:
            coord_pubkey = get_pubkey(coord_identity)
        except Exception:
            coord_pubkey = ""
    invite = {
        "type": "diloco-invite-v1",
        "run_id": cfg.run_id,
        "coordinator_pubkey": coord_pubkey,
        "topic": (args.p2p_topic or ""),
        "context": p2p_context or cfg.run_id,
        "config_hash": cfg.fingerprint(),
        "base_hash": str(getattr(coordinator, "base_hash", "") or ""),
        "transport": cfg.coordinator.transport,
        "endpoint": f"http://{cfg.coordinator.host}:{port}",
        "expires_at": int(_time.time()) + 24 * 3600,
        "allowed_node_pubkeys": list(cfg.coordinator.allowed_node_pubkeys or []),
        "require_node_identity": bool(cfg.coordinator.require_node_identity),
    }
    if coord_identity and coord_pubkey:
        try:
            msg = canonical_invite(invite)
            sig = sign_message(coord_identity, msg)
            invite["sig"] = sig.get("sig", "")
            invite["sig_alg"] = sig.get("alg", "")
            invite["signed_by"] = coord_pubkey
        except Exception:
            pass
    return invite


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
    if args.tls_cert:
        coordinator_cfg = replace(coordinator_cfg, tls_cert=args.tls_cert)
    if args.tls_key:
        coordinator_cfg = replace(coordinator_cfg, tls_key=args.tls_key)
    if args.tls_client_ca:
        coordinator_cfg = replace(coordinator_cfg, tls_client_ca=args.tls_client_ca)
    if args.tls_require_client_cert:
        coordinator_cfg = replace(coordinator_cfg, tls_require_client_cert=True)

    allowed = list(coordinator_cfg.allowed_node_pubkeys or [])
    allowed.extend([str(x).strip() for x in (args.allow_pubkey or []) if str(x).strip()])
    for p in (args.allow_pubkey_file or []):
        path = Path(p)
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        try:
            data = json.loads(text)
            if isinstance(data, list):
                allowed.extend([str(x).strip() for x in data if str(x).strip()])
            elif isinstance(data, dict) and data.get("pubkey"):
                allowed.append(str(data["pubkey"]).strip())
            else:
                allowed.extend([line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")])
        except json.JSONDecodeError:
            allowed.extend([line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")])
    # de-dup preserve order
    seen = set()
    deduped = []
    for k in allowed:
        if k and k not in seen:
            seen.add(k)
            deduped.append(k)
    if deduped:
        coordinator_cfg = replace(coordinator_cfg, allowed_node_pubkeys=tuple(deduped))
    if args.require_node_identity:
        coordinator_cfg = replace(coordinator_cfg, require_node_identity=True)
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
        init_state_arg = (args.init_state or "").strip()
        auto_init = None
        if init_state_arg in ("auto", "") and cfg.model.kind == "hf":
            auto_init = (PROJECT_ROOT / "runs" / f"init-{config_path.stem}").resolve()
            if auto_init.joinpath("meta.json").is_file():
                init_state_arg = str(auto_init)
                logging.info("using discovered init state: %s", init_state_arg)
            elif init_state_arg == "auto":
                logging.info("exporting initial adapter state to %s", auto_init)
                state, base_hash, adapter_meta = initial_adapter_state(cfg, device=args.device)
                save_initial_state(auto_init, state, base_hash, adapter_meta, cfg.fingerprint())
                init_state_arg = str(auto_init)
        if init_state_arg and init_state_arg != "auto":
            init_path = Path(init_state_arg)
            if not init_path.is_absolute():
                init_path = (PROJECT_ROOT / init_path).resolve()
            state, base_hash, adapter_meta, init_meta = load_initial_state(init_path)
            if init_meta.get("config_hash") != cfg.fingerprint():
                raise SystemExit("initial-state config hash does not match this config")
        elif cfg.model.kind == "hf":
            raise SystemExit(
                "for kind='hf' export the initial adapter first: "
                "scripts/export_initial_state.py --config ... --out runs/init-<config>, "
                "or pass --init-state auto / --init-state runs/init-<config>"
            )
        else:
            state, base_hash, adapter_meta = initial_adapter_state(cfg, device=args.device)
        coordinator = Coordinator(cfg, state, base_hash, adapter_meta)

    port = coordinator.start()
    print(f"PORT={port}", flush=True)
    print(f"COORDINATOR_URL=http://{cfg.coordinator.host}:{port}", flush=True)

    coord_identity = (args.coordinator_identity or "").strip()
    if coord_identity:
        ip = Path(coord_identity)
        if not ip.is_absolute():
            ip = (PROJECT_ROOT / ip).resolve()
        coord_identity = str(ip)
    p2p_context = (args.p2p_context or cfg.run_id or (args.p2p_topic or "")).strip()

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
        peer_pubkeys = list(cfg.coordinator.allowed_node_pubkeys or [])
        if coord_identity and peer_pubkeys:
            # Export effective allowlist so identity tunnel can hot-add/revoke peers from file updates
            peer_file = checkpoint_dir / "allowed_peer_pubkeys.txt"
            try:
                peer_file.parent.mkdir(parents=True, exist_ok=True)
                peer_file.write_text("\n".join(str(p).strip() for p in peer_pubkeys if str(p).strip()) + "\n", encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                logging.warning("could not write tunnel peer allowlist file %s: %s", peer_file, exc)
                peer_file = None
            tunnel = HolepunchSidecar(
                mode="tunnel-server",
                topic=args.p2p_topic,
                name=args.sidecar_name,
                target_host=args.p2p_target_host,
                target_port=port,
                identity=coord_identity,
                peer_pubkeys=peer_pubkeys,
                peer_pubkey_files=[str(peer_file)] if peer_file else None,
                context=p2p_context,
            )
            tunnel.start()
            tunnel.wait_for_ready()
            sidecars.append(tunnel)
            logging.info(
                "coordinator available through identity bitbeam tunnel (%s peers, context=%s, allowlist_file=%s)",
                len(peer_pubkeys), p2p_context, peer_file,
            )
        else:
            if coord_identity and not peer_pubkeys:
                logging.warning("--coordinator-identity set but no allowed peer pubkeys; falling back to topic tunnel")
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

    if args.print_invite:
        try:
            invite = _build_invite(cfg, coordinator, port, args, coord_identity, p2p_context)
            out = args.print_invite
            text = json.dumps(invite, indent=2, sort_keys=True)
            if out == "-":
                print(text, flush=True)
            else:
                out_path = Path(out)
                if not out_path.is_absolute():
                    out_path = (PROJECT_ROOT / out_path).resolve()
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(text + "\n", encoding="utf-8")
                logging.info("invite written to %s", out_path)
        except Exception as exc:  # noqa: BLE001
            logging.exception("failed to generate invite: %s", exc)
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
