#!/usr/bin/env python3
"""Convenience orchestrator for DiLoCo pilots (meshllm-style UX helpers)."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _p(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath(*parts)


def _run(cmd: List[str], check: bool = False) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=check)


def cmd_preflight(args: argparse.Namespace) -> int:
    ok = True
    print("== DiLoCo pilot preflight ==")
    print(f"python: {PYTHON}")
    print(f"project: {PROJECT_ROOT}")

    node = shutil.which("node")
    if node:
        v = subprocess.run([node, "--version"], capture_output=True, text=True)
        print(f"node: {node} ({v.stdout.strip()})")
    else:
        print("node: MISSING (required for bitbeam tunnels/identity signing)")
        ok = False

    sidecar_pkg = _p("network", "holepunch-sidecar", "package.json")
    sidecar_nm = _p("network", "holepunch-sidecar", "node_modules")
    print(f"sidecar package: {'ok' if sidecar_pkg.exists() else 'missing'}")
    print(f"sidecar node_modules: {'ok' if sidecar_nm.exists() else 'missing (run: cd network/holepunch-sidecar && npm install)'}")
    if not sidecar_nm.exists():
        ok = False

    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.is_absolute():
            cfg_path = PROJECT_ROOT / cfg_path
        print(f"config: {cfg_path} {'ok' if cfg_path.exists() else 'MISSING'}")
        if not cfg_path.exists():
            ok = False
        else:
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                if str(data.get("model", {}).get("kind", "toy")) == "hf":
                    init_dir = _p("runs", f"init-{cfg_path.stem}")
                    meta = init_dir / "meta.json"
                    print(f"init-state ({init_dir.name}): {'ok' if meta.exists() else 'missing (use --init-state auto or export_initial_state.py)'}")
            except Exception as exc:
                print(f"config parse warning: {exc}")

    print("result:", "OK" if ok else "ISSUES FOUND")
    return 0 if ok else 1


def cmd_identity(args: argparse.Namespace) -> int:
    cmd = [PYTHON, "scripts/identity_init.py", "--name", args.name]
    if args.out:
        cmd += ["--out", args.out]
    return _run(cmd).returncode


def cmd_up(args: argparse.Namespace) -> int:
    # Basic orchestrator: ensure identity optional, then launch coordinator
    config = args.config
    if not config:
        print("--config is required", file=sys.stderr)
        return 2

    cmd = [PYTHON, "scripts/run_coordinator.py", "--config", config]
    passthrough = {
        "device": "--device",
        "checkpoint_dir": "--checkpoint-dir",
        "init_state": "--init-state",
        "max_rounds": "--max-rounds",
        "p2p_topic": "--p2p-topic",
        "coordinator_identity": "--coordinator-identity",
        "p2p_context": "--p2p-context",
        "print_invite": "--print-invite",
    }
    for attr, flag in passthrough.items():
        val = getattr(args, attr, None)
        if val is None:
            continue
        if isinstance(val, bool):
            if val:
                cmd.append(flag)
        elif str(val) != "" and str(val) != "-1":
            cmd += [flag, str(val)]
    if args.no_wait:
        cmd.append("--no-wait")
    if args.no_resume:
        cmd.append("--no-resume")
    if args.reset:
        cmd.append("--reset")
    if args.require_node_identity:
        cmd.append("--require-node-identity")
    for pk in (args.allow_pubkey or []):
        cmd += ["--allow-pubkey", pk]
    for pf in (args.allow_pubkey_file or []):
        cmd += ["--allow-pubkey-file", pf]

    # convenience: auto identity generation if requested and missing
    if args.coordinator_identity:
        ident_path = Path(args.coordinator_identity)
        if not ident_path.is_absolute():
            ident_path = PROJECT_ROOT / ident_path
        if not ident_path.exists():
            name = ident_path.stem or "coordinator"
            print(f"coordinator identity not found, generating: {ident_path}")
            rc = _run([PYTHON, "scripts/identity_init.py", "--name", name, "--out", str(ident_path)]).returncode
            if rc != 0:
                return rc

    print("== launching coordinator ==")
    proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT))

    def _stop(*_a):
        try:
            proc.send_signal(signal.SIGINT)
        except Exception:
            pass

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    return proc.wait()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="DiLoCo pilot helper")
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("preflight", help="check local prerequisites")
    pf.add_argument("--config", default="")
    pf.set_defaults(func=cmd_preflight)

    ident = sub.add_parser("identity", help="generate a node identity")
    ident.add_argument("--name", required=True)
    ident.add_argument("--out", default="")
    ident.set_defaults(func=cmd_identity)

    up = sub.add_parser("up", help="launch coordinator with common pilot defaults")
    up.add_argument("--config", required=True)
    up.add_argument("--device", default="auto")
    up.add_argument("--checkpoint-dir", dest="checkpoint_dir", default="")
    up.add_argument("--init-state", dest="init_state", default="auto")
    up.add_argument("--max-rounds", dest="max_rounds", type=int, default=-1)
    up.add_argument("--no-wait", dest="no_wait", action="store_true")
    up.add_argument("--no-resume", dest="no_resume", action="store_true")
    up.add_argument("--reset", action="store_true")
    up.add_argument("--p2p-topic", dest="p2p_topic", default="")
    up.add_argument("--p2p-context", dest="p2p_context", default="")
    up.add_argument("--coordinator-identity", dest="coordinator_identity", default="")
    up.add_argument("--allow-pubkey", dest="allow_pubkey", action="append", default=[])
    up.add_argument("--allow-pubkey-file", dest="allow_pubkey_file", action="append", default=[])
    up.add_argument("--require-node-identity", dest="require_node_identity", action="store_true")
    up.add_argument("--print-invite", dest="print_invite", default="")
    up.set_defaults(func=cmd_up)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
