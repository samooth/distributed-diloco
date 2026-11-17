#!/usr/bin/env python3
"""One-command orchestrator for DiLoCo pilots (meshllm-style UX).

No config files or allowlists need to be edited by hand:

    pilot.py init  --name demo --preset toy
    pilot.py up    --name demo --preset toy --workers 2
    pilot.py join  'didiloco://join?d=...' --node-id worker-a
    pilot.py status --url http://127.0.0.1:8095

`up` creates identity + config from a preset, starts the coordinator with a
signed invite and prints a single join code (and QR when a backend is present).
`join` verifies the invite, gets the worker admitted via /v1/join, derives all
flags from the invite and launches the worker.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from diloco.config import config_from_dict, validate_config  # noqa: E402
from diloco.identity import canonical_join, get_pubkey, sign_message  # noqa: E402
from diloco.invite import (  # noqa: E402
    InviteError,
    friendly_error,
    make_join_code,
    parse_join_code,
    verify_invite,
)
from diloco.presets import build_preset, preset_names  # noqa: E402
from diloco.qrcode_util import write_qr  # noqa: E402
from diloco.tunnel_allowlist import normalize_pubkeys  # noqa: E402


def _p(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath(*parts)


def _run(cmd: List[str], check: bool = False) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=check)


def _http_json(method: str, url: str, payload: Optional[Dict[str, Any]] = None, token: str = "", timeout: float = 15.0) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-DiLoCo-Token"] = token
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            message = str(json.loads(raw.decode("utf-8")).get("error", raw.decode("utf-8")))
        except Exception:
            message = raw.decode("utf-8", errors="replace")
        raise InviteError(friendly_error(f"HTTP {exc.code}: {message}")) from exc
    except urllib.error.URLError as exc:
        raise InviteError(friendly_error(f"cannot reach coordinator: {exc.reason}")) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise InviteError(f"respuesta no válida del coordinador: {exc}") from exc


def _ensure_identity(path: Path, name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
        raise InviteError("nombre de nodo no válido (usa letras, números, . _ -)")
    path = path if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        rc = _run([PYTHON, "scripts/identity_init.py", "--name", name, "--out", str(path)]).returncode
        if rc != 0:
            raise InviteError("no se pudo crear la identidad BSV")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return get_pubkey(path)


def _ensure_config(path: Path, preset: str, overrides: Dict[str, Any]) -> str:
    path = path if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = build_preset(preset, **overrides)
    validate_config(config_from_dict(raw))
    path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(path)


def cmd_preflight(args: argparse.Namespace) -> int:
    ok = True
    print("== DiLoCo pilot preflight ==")
    print(f"python: {PYTHON}")
    print(f"project: {PROJECT_ROOT}")
    node = shutil.which("node")
    print(f"node: {node or 'MISSING (required for bitbeam identities)'}") if node else None
    if not node:
        ok = False
    sidecar_pkg = _p("network", "holepunch-sidecar", "package.json")
    sidecar_nm = _p("network", "holepunch-sidecar", "node_modules")
    print(f"sidecar package: {'ok' if sidecar_pkg.exists() else 'missing'}")
    print(f"sidecar node_modules: {'ok' if sidecar_nm.exists() else 'missing (npm install in network/holepunch-sidecar)'}")
    if not sidecar_nm.exists():
        ok = False
    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.is_absolute():
            cfg_path = PROJECT_ROOT / cfg_path
        print(f"config: {cfg_path} {'ok' if cfg_path.exists() else 'MISSING'}")
        if not cfg_path.exists():
            ok = False
    print("result:", "OK" if ok else "ISSUES FOUND")
    return 0 if ok else 1


def cmd_identity(args: argparse.Namespace) -> int:
    cmd = [PYTHON, "scripts/identity_init.py", "--name", args.name]
    if args.out:
        cmd += ["--out", args.out]
    return _run(cmd).returncode


def cmd_init(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else Path("runs") / args.name
    out_dir = out_dir if out_dir.is_absolute() else (PROJECT_ROOT / out_dir).resolve()
    overrides: Dict[str, Any] = {
        "run_id": args.name,
        "data": {"path": args.data} if args.data else None,
        "train": {"batch_size": args.batch_size} if args.batch_size else None,
        "coordinator": {
            "min_workers": args.workers,
            "max_workers": max(args.workers, 4),
            "quorum": args.workers,
            "max_rounds": args.rounds,
        },
    }
    cfg_path = _ensure_config(out_dir / "config.json", args.preset, overrides)
    pubkey = _ensure_identity(out_dir / "identity.json", args.name)
    print(f"config:  {cfg_path}")
    print(f"identity: {out_dir / 'identity.json'}")
    print(f"pubkey:  {pubkey}")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else Path("runs") / args.name
    out_dir = out_dir if out_dir.is_absolute() else (PROJECT_ROOT / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    overrides: Dict[str, Any] = {
        "run_id": args.name,
        "data": {"path": args.data} if args.data else None,
        "coordinator": {
            "min_workers": args.workers,
            "max_workers": max(args.workers, 4),
            "quorum": args.workers,
            "max_rounds": args.rounds,
        },
    }
    cfg_path = _ensure_config(out_dir / "config.json", args.preset, overrides)
    identity_path = out_dir / "identity.json"
    pubkey = _ensure_identity(identity_path, args.name)
    topic = args.p2p_topic or f"didiloco-{args.name}"
    context = args.p2p_context or topic
    token = secrets.token_urlsafe(24)
    invite_path = out_dir / "invite.json"

    cmd = [
        PYTHON, "scripts/run_coordinator.py",
        "--config", cfg_path,
        "--device", args.device,
        "--coordinator-identity", str(identity_path),
        "--p2p-topic", topic,
        "--p2p-context", context,
        "--require-node-identity",
        "--join-token", token,
        "--join-max-uses", str(args.workers),
        "--join-ttl-s", str(args.ttl),
        "--print-invite", str(invite_path),
        "--checkpoint-dir", str(out_dir / "checkpoints"),
        "--no-wait",
    ]
    if args.no_resume:
        cmd.append("--no-resume")
    if args.reset:
        cmd.append("--reset")
    print("== launching coordinator ==")
    proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT))

    def _stop(*_: Any) -> None:
        try:
            proc.send_signal(signal.SIGINT)
        except Exception:
            pass

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    deadline = time.time() + 30
    while time.time() < deadline and not invite_path.is_file():
        if proc.poll() is not None:
            print("coordinator exited before writing the invite", file=sys.stderr)
            return proc.returncode or 1
        time.sleep(0.2)
    if not invite_path.is_file():
        print("timed out waiting for invite.json", file=sys.stderr)
        _stop()
        return 1

    invite = parse_join_code(invite_path, verify=True)
    code = make_join_code(invite)
    join_path = out_dir / "join.txt"
    join_path.write_text(code + "\n", encoding="utf-8")
    qr_path = out_dir / "join.svg"
    if write_qr(code, qr_path):
        print(f"QR: {qr_path}")
    print("\n== Comparte esta ÚNICA línea con los workers ==")
    print(code)
    print(f"\n(también en {join_path})")
    print("En cada worker:  pilot.py join '<la línea>' --node-id <nombre>")
    print("Pulsa Ctrl+C para detener el coordinador.\n")
    return proc.wait()


def cmd_join(args: argparse.Namespace) -> int:
    try:
        invite = parse_join_code(args.code, verify=True)
    except InviteError as exc:
        print(f"ERROR: {friendly_error(str(exc))}", file=sys.stderr)
        return 2
    token = str(invite.get("join_token", "") or "")
    endpoint = str(invite.get("endpoint", "") or "").rstrip("/")
    config_hash = str(invite.get("config_hash", "") or "")
    base_hash = str(invite.get("base_hash", "") or "")
    run_id = str(invite.get("run_id", "") or "")
    if not token or not endpoint:
        print("ERROR: la invitación no contiene token o endpoint", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir).expanduser() if args.out_dir else Path("runs") / args.node_id
    out_dir = out_dir if out_dir.is_absolute() else (PROJECT_ROOT / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    identity_path = Path(args.node_identity).expanduser() if args.node_identity else out_dir / "identity.json"
    pubkey = _ensure_identity(identity_path, args.node_id)

    payload = {
        "type": "diloco-join-v1",
        "run_id": run_id,
        "node_id": args.node_id,
        "pubkey": pubkey,
        "token": token,
        "config_hash": config_hash,
        "base_hash": base_hash,
    }
    signed = sign_message(str(identity_path), canonical_join(payload))
    try:
        result = _http_json("POST", endpoint + "/v1/join", {
            **payload,
            "node_sig": signed.get("sig", ""),
            "sig_alg": signed.get("alg", ""),
        })
    except InviteError as exc:
        print(f"ERROR: {friendly_error(str(exc))}", file=sys.stderr)
        return 2
    print(f"Admitido en el run {run_id} · ronda {result.get('round', '?')}")

    # Write the exact signed config locally so the worker fingerprint matches.
    config_json = invite.get("config_json")
    if config_json:
        cfg_path = out_dir / "config.json"
        cfg_path.write_text(json.dumps(json.loads(config_json), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        cfg_path = Path(args.config or "configs/smoke.json")

    state_cache = str(out_dir / "state-cache")
    cmd = [
        PYTHON, "scripts/run_worker.py",
        "--config", str(cfg_path),
        "--node-id", args.node_id,
        "--shard-id", str(args.shard_id),
        "--device", args.device,
        "--node-identity", str(identity_path),
        "--invite", str(out_dir / "invite.json"),
        "--state-cache-dir", state_cache,
        "--log", str(out_dir / "worker.log"),
    ]
    if args.no_tunnel:
        cmd.append("--no-tunnel")
    (out_dir / "invite.json").write_text(json.dumps(invite, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.no_launch:
        print("join completado (--no-launch): no se lanza el worker")
        print(" ".join(cmd))
        return 0
    print("== launching worker ==")
    proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT))

    def _stop(*_: Any) -> None:
        try:
            proc.send_signal(signal.SIGINT)
        except Exception:
            pass

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    return proc.wait()


def cmd_status(args: argparse.Namespace) -> int:
    url = args.url.rstrip("/")
    headers = {}
    if args.token:
        headers["X-DiLoCo-Token"] = args.token
    req = urllib.request.Request(url + "/v1/status", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {friendly_error(str(exc))}", file=sys.stderr)
        return 2
    round_id = int(data.get("round", 0))
    max_rounds = int(data.get("max_rounds", 0))
    print(f"run: {data.get('run_id', '?')}")
    print(f"ronda: {round_id}/{max_rounds}")
    print(f"workers: {len(data.get('registered') or [])} (quórum {data.get('quorum', '?')})")
    print(f"esperando: {data.get('waiting_workers', '?')} · timeout efectivo: {data.get('effective_round_timeout_s', '?')}s")
    if data.get("failure"):
        print(f"fallo: {friendly_error(str(data['failure']))}")
    last = data.get("last_result") or {}
    if last:
        print(
            "última ronda: loss={} delta={} sync={}s partial={}".format(
                last.get("train_loss"), last.get("delta_norm"),
                last.get("sync_seconds"), last.get("partial", False),
            )
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DiLoCo pilot (one-command UX)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("preflight", help="check local prerequisites")
    p.add_argument("--config", default="")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("identity", help="generate a node identity")
    p.add_argument("--name", required=True)
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_identity)

    p = sub.add_parser("init", help="create identity + config from a preset")
    p.add_argument("--name", required=True)
    p.add_argument("--preset", default="toy", choices=preset_names())
    p.add_argument("--out-dir", default="")
    p.add_argument("--data", default="")
    p.add_argument("--batch-size", type=int, default=0)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--rounds", type=int, default=4)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("up", help="launch coordinator and print a join code")
    p.add_argument("--name", default="demo")
    p.add_argument("--preset", default="toy", choices=preset_names())
    p.add_argument("--out-dir", default="")
    p.add_argument("--data", default="")
    p.add_argument("--device", default="cpu")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--rounds", type=int, default=4)
    p.add_argument("--ttl", type=int, default=3600)
    p.add_argument("--p2p-topic", default="")
    p.add_argument("--p2p-context", default="")
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--reset", action="store_true")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("join", help="join a run using a signed invite code")
    p.add_argument("code")
    p.add_argument("--node-id", default="worker-0")
    p.add_argument("--shard-id", type=int, default=0)
    p.add_argument("--device", default="cpu")
    p.add_argument("--config", default="")
    p.add_argument("--node-identity", default="")
    p.add_argument("--out-dir", default="")
    p.add_argument("--no-launch", action="store_true")
    p.add_argument("--no-tunnel", action="store_true", help="connect directly to the invite endpoint (LAN/dev)")
    p.set_defaults(func=cmd_join)

    p = sub.add_parser("status", help="show friendly run status")
    p.add_argument("--url", default="http://127.0.0.1:8095")
    p.add_argument("--token", default="")
    p.set_defaults(func=cmd_status)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args) or 0)
    except InviteError as exc:
        print(f"ERROR: {friendly_error(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
