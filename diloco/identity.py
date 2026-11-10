"""Helpers for BSV identity signing/verification via the sidecar."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
IDENTITY_JS = PROJECT_ROOT / "network" / "holepunch-sidecar" / "identity.js"


def _node_bin() -> str:
    return shutil.which("node") or "node"


def sign_message(identity_file: str | Path, message: str) -> Dict[str, Any]:
    cmd = [_node_bin(), str(IDENTITY_JS), "--cmd", "sign", "--file", str(identity_file), "--message", message]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "sign failed").strip())
    return json.loads(proc.stdout.strip() or "{}")


def verify_message(pubkey: str, message: str, sig: str) -> bool:
    if not pubkey or not sig:
        return False
    cmd = [_node_bin(), str(IDENTITY_JS), "--cmd", "verify", "--pubkey", pubkey, "--message", message, "--sig", sig]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return False
    try:
        data = json.loads(proc.stdout.strip() or "{}")
        return bool(data.get("ok"))
    except Exception:
        return False


def get_pubkey(identity_file: str | Path) -> str:
    data = json.loads(Path(identity_file).read_text(encoding="utf-8"))
    return str(data.get("pubkey", "")).strip()


def canonical_register(node_id: str, config_hash: str, base_hash: str, run_id: str = "") -> str:
    return f"diloco.register|node_id={node_id}|config_hash={config_hash}|base_hash={base_hash}|run_id={run_id}"


def canonical_update(node_id: str, round_id: int, update_hash: str, config_hash: str = "", state_hash: str = "") -> str:
    return (
        f"diloco.update|node_id={node_id}|round={int(round_id)}|update_hash={update_hash}"
        f"|config_hash={config_hash}|state_hash={state_hash}"
    )


def canonical_invite(payload: Dict[str, Any]) -> str:
    # Deterministic subset for invite signing
    keys = [
        "type", "run_id", "coordinator_pubkey", "topic", "context",
        "config_hash", "base_hash", "transport", "expires_at",
        "allowed_node_pubkeys", "endpoint",
    ]
    parts = []
    for k in keys:
        if k in payload and payload[k] is not None:
            v = payload[k]
            if isinstance(v, (list, tuple)):
                v = ",".join(str(x) for x in v)
            parts.append(f"{k}={v}")
    return "diloco.invite|" + "|".join(parts)
