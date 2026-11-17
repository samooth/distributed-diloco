"""P2.5: allowlist ↔ identity-tunnel peer-file sync tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diloco.tunnel_allowlist import (  # noqa: E402
    normalize_pubkeys,
    read_peer_allowlist,
    reconcile_peers,
    write_peer_allowlist,
)


def _key(seed: str) -> str:
    # Base64-like key satisfying the shape validation (>=16 chars).
    return ("diloco" + seed) * 4


class TunnelAllowlistTests(unittest.TestCase):
    def test_normalize_trims_dedupes_and_drops_blanks(self):
        a, b = _key("a"), _key("b")
        result = normalize_pubkeys([f"  {a}  ", "", None, a, b, "  "])
        self.assertEqual(result, [a, b])

    def test_normalize_rejects_invalid_key(self):
        with self.assertRaises(ValueError):
            normalize_pubkeys(["short"])

    def test_write_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "allowed_peer_pubkeys.txt"
            a, b = _key("a"), _key("b")
            self.assertTrue(write_peer_allowlist(path, [a, b]))
            self.assertEqual(path.read_text(encoding="utf-8"), f"{a}\n{b}\n")
            # Second write with same content is a no-op.
            self.assertFalse(write_peer_allowlist(path, [b, a]))
            # No temp files left behind.
            self.assertEqual(list(path.parent.glob("*.tmp-*")), [])
            self.assertEqual(read_peer_allowlist(path), [a, b])

    def test_reconcile_add_remove_deltas(self):
        a, b, c = _key("a"), _key("b"), _key("c")
        add, remove = reconcile_peers([a, b], [b, c])
        self.assertEqual(add, [c])
        self.assertEqual(remove, [a])
        add, remove = reconcile_peers([a], [a])
        self.assertEqual((add, remove), ([], []))

    def test_update_file_reflects_allowlist_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "peers.txt"
            a, b, c = _key("a"), _key("b"), _key("c")
            write_peer_allowlist(path, [a, b])
            want = read_peer_allowlist(path)
            changed = write_peer_allowlist(path, [b, c])
            self.assertTrue(changed)
            add, remove = reconcile_peers(want, read_peer_allowlist(path))
            self.assertEqual(add, [c])
            self.assertEqual(remove, [a])


if __name__ == "__main__":
    unittest.main()
