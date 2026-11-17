"""Invites, signed join codes and presets tests."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diloco.identity import canonical_invite, get_pubkey, sign_message  # noqa: E402
from diloco.invite import (  # noqa: E402
    InviteError,
    friendly_error,
    make_join_code,
    parse_join_code,
    verify_invite,
)
from diloco.presets import build_preset, preset_names  # noqa: E402


class InviteTests(unittest.TestCase):
    def _identity(self, tmp: Path, name: str) -> Path:
        import subprocess

        path = tmp / f"{name}.json"
        subprocess.run(
            [
                "node", str(ROOT / "network/holepunch-sidecar/identity.js"),
                "--cmd", "generate", "--name", name, "--out", str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return path

    def _invite(self, tmp: Path, **overrides: Any) -> Dict[str, Any]:
        ident = self._identity(tmp, "coord-invite")
        base = {
            "type": "diloco-invite-v1",
            "run_id": "unit-invite",
            "coordinator_pubkey": get_pubkey(ident),
            "topic": "didiloco-unit",
            "context": "didiloco-unit",
            "config_hash": "cfg",
            "base_hash": "base",
            "transport": "http",
            "endpoint": "http://127.0.0.1:8095",
            "expires_at": int(time.time()) + 600,
            "join_token": "secret-token",
            "max_uses": 2,
            "config_json": '{"run_id":"unit-invite"}',
        }
        base.update(overrides)
        unsigned = {k: v for k, v in base.items() if k not in ("sig", "sig_alg", "signed_by")}
        signed = sign_message(ident, canonical_invite(unsigned))
        base.update({"sig": signed.get("sig", ""), "sig_alg": signed.get("alg", ""), "signed_by": get_pubkey(ident)})
        return base

    def test_valid_invite_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            invite = self._invite(Path(tmp))
            self.assertTrue(verify_invite(invite))

    def test_invalid_signature_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            invite = self._invite(Path(tmp))
            invite["endpoint"] = "http://evil.example:9999"
            self.assertFalse(verify_invite(invite))
            with self.assertRaises(InviteError):
                parse_join_code(invite)

    def test_expired_invite_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ident = self._identity(Path(tmp), "coord-expired")
            payload = {
                "type": "diloco-invite-v1", "run_id": "r", "expires_at": 1,
                "endpoint": "http://127.0.0.1:1", "join_token": "t",
            }
            signed = sign_message(ident, canonical_invite(payload))
            payload.update({"sig": signed["sig"], "signed_by": get_pubkey(ident)})
            self.assertFalse(verify_invite(payload, now=time.time()))

    def test_unsigned_invite_requires_sig(self):
        with tempfile.TemporaryDirectory() as tmp:
            invite = self._invite(Path(tmp))
            invite.pop("sig")
            invite.pop("signed_by")
            self.assertFalse(verify_invite(invite, require_sig=True))
            self.assertTrue(verify_invite(invite, require_sig=False))

    def test_short_code_roundtrip_with_stubbed_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            invite = self._invite(Path(tmp))
            code = make_join_code(invite)
            self.assertTrue(code.startswith("didiloco://join?"))
            self.assertLess(len(code), 300)
            import diloco.invite as invite_mod

            original = invite_mod.fetch_invite
            try:
                invite_mod.fetch_invite = lambda endpoint, token: dict(invite)
                parsed = parse_join_code(code)
                self.assertEqual(parsed["join_token"], "secret-token")
                self.assertEqual(parsed["run_id"], "unit-invite")
            finally:
                invite_mod.fetch_invite = original

    def test_short_code_token_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            invite = self._invite(Path(tmp))
            code = make_join_code(invite)
            import diloco.invite as invite_mod

            original = invite_mod.fetch_invite
            try:
                invite_mod.fetch_invite = lambda endpoint, token: {**invite, "join_token": "other"}
                with self.assertRaises(InviteError):
                    parse_join_code(code)
            finally:
                invite_mod.fetch_invite = original

    def test_friendly_errors(self):
        self.assertIn("autorizada", friendly_error("node pubkey is not allowed"))
        self.assertIn("configuración", friendly_error("config fingerprint mismatch"))
        self.assertIn("firma", friendly_error("invalid node signature"))
        self.assertEqual(friendly_error("unknown"), "unknown")

    def test_presets_are_valid_and_overridable(self):
        from diloco.config import config_from_dict, validate_config

        self.assertIn("toy", preset_names())
        self.assertIn("70b-qlora", preset_names())
        for name in preset_names():
            cfg = config_from_dict(build_preset(name))
            validate_config(cfg)
        cfg = config_from_dict(build_preset("toy", run_id="custom", coordinator={"rounds": 9, "max_rounds": 9}))
        self.assertEqual(cfg.run_id, "custom")
        self.assertEqual(cfg.coordinator.max_rounds, 9)
        with self.assertRaises(ValueError):
            build_preset("does-not-exist")


if __name__ == "__main__":
    unittest.main()
