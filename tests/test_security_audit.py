"""Security audit checks for the one-command invite flow."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diloco.identity import canonical_invite, get_pubkey, sign_message  # noqa: E402
from diloco.invite import InviteError, friendly_error, make_join_code, parse_join_code, verify_invite  # noqa: E402
from scripts.pilot import _ensure_identity  # noqa: E402


class SecurityAuditTests(unittest.TestCase):
    def test_identity_file_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "id.json"
            _ensure_identity(path, "audit-node")
            self.assertTrue(path.is_file())
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600, f"identity file mode is {oct(mode)}")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("wif", data)
            self.assertNotEqual(get_pubkey(path), data["wif"])

    def test_node_name_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(InviteError):
                _ensure_identity(Path(tmp) / "x.json", "../../etc/passwd")

    def test_tampered_config_json_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ident = Path(tmp) / "coord.json"
            import subprocess

            subprocess.run(
                ["node", str(ROOT / "network/holepunch-sidecar/identity.js"),
                 "--cmd", "generate", "--name", "coord-audit", "--out", str(ident)],
                check=True, capture_output=True, text=True,
            )
            payload = {
                "type": "diloco-invite-v1", "run_id": "r", "endpoint": "http://127.0.0.1:1",
                "join_token": "t", "config_json": '{"run_id":"r"}',
            }
            signed = sign_message(ident, canonical_invite(payload))
            payload.update({"sig": signed["sig"], "signed_by": get_pubkey(ident)})
            self.assertTrue(verify_invite(payload))
            payload["config_json"] = '{"run_id":"attacker"}'
            self.assertFalse(verify_invite(payload), "tampered config must invalidate signature")

    def test_join_code_file_is_private_and_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            import subprocess

            ident = Path(tmp) / "coord2.json"
            subprocess.run(
                ["node", str(ROOT / "network/holepunch-sidecar/identity.js"),
                 "--cmd", "generate", "--name", "coord2", "--out", str(ident)],
                check=True, capture_output=True, text=True,
            )
            payload = {
                "type": "diloco-invite-v1", "run_id": "r", "endpoint": "http://127.0.0.1:1",
                "join_token": "secret-token", "config_json": "{}",
            }
            signed = sign_message(ident, canonical_invite(payload))
            payload.update({"sig": signed["sig"], "signed_by": get_pubkey(ident)})
            code = make_join_code(payload)
            code_path = Path(tmp) / "join.code"
            code_path.write_text(code + "\n", encoding="utf-8")
            os.chmod(code_path, 0o644)
            import diloco.invite as im

            original = im.fetch_invite
            try:
                im.fetch_invite = lambda endpoint, token: dict(payload)
                parsed = parse_join_code(code_path)
                self.assertEqual(parsed["join_token"], "secret-token")
            finally:
                im.fetch_invite = original
            self.assertEqual(stat.S_IMODE(code_path.stat().st_mode), 0o600)

    def test_friendly_errors_never_echo_secrets(self):
        secret = "SUPER_SECRET_TOKEN_12345"
        out = friendly_error(f"HTTP 403: invalid token {secret}")
        self.assertNotIn(secret, out)

    def test_secrets_are_gitignored(self):
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("deploy/.env", text)
        self.assertIn("runs/", text)


if __name__ == "__main__":
    unittest.main()
