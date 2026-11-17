"""Allowlist ↔ identity-tunnel peer-file synchronization.

The coordinator exports its effective ``allowed_node_pubkeys`` to a peer file
consumed by ``tunnel-server-id`` (``--peer-pubkey-file``). This module makes that
export atomic and idempotent, and exposes a pure reconciliation helper used to
compute late-join/revocation deltas. It is deliberately dependency-free so it
can be unit-tested without bitbeam/Node.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, List, Set, Tuple, Union

_PUBKEY_RE = re.compile(r"^[A-Za-z0-9+/=_-]{16,256}$")


def normalize_pubkeys(values: Iterable[object]) -> List[str]:
    """Trim, drop blanks, validate shape, dedupe while preserving order."""
    out: List[str] = []
    seen: Set[str] = set()
    for value in values:
        key = str(value or "").strip()
        if not key or key in seen:
            continue
        if not _PUBKEY_RE.match(key):
            raise ValueError(f"invalid peer pubkey: {key[:32]!r}")
        seen.add(key)
        out.append(key)
    return out


def read_peer_allowlist(path: Union[str, os.PathLike[str]]) -> List[str]:
    p = Path(path)
    if not p.is_file():
        return []
    return normalize_pubkeys(p.read_text(encoding="utf-8").splitlines())


def write_peer_allowlist(path: Union[str, os.PathLike[str]], pubkeys: Iterable[object]) -> bool:
    """Atomically write the peer file. Returns True if content changed."""
    # Canonical (sorted) order makes the file idempotent regardless of input order.
    keys = sorted(normalize_pubkeys(pubkeys))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(keys) + ("\n" if keys else "")
    if target.is_file() and target.read_text(encoding="utf-8") == content:
        return False
    tmp = target.with_name(target.name + f".tmp-{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)
    return True


def reconcile_peers(current: Iterable[object], desired: Iterable[object]) -> Tuple[List[str], List[str]]:
    """Return (to_add, to_remove) for a peer-set reconciliation."""
    cur = set(normalize_pubkeys(current))
    des = set(normalize_pubkeys(desired))
    return sorted(des - cur), sorted(cur - des)
