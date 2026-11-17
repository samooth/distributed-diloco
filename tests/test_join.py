"""Invite-based admission (/v1/join, gRPC Join) and security tests."""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diloco.backend import initial_adapter_state  # noqa: E402
from diloco.config import CoordinatorConfig, DataConfig, ExperimentConfig, ModelConfig, TrainConfig  # noqa: E402
from diloco.coordinator import ApiError, Coordinator  # noqa: E402
from diloco.identity import canonical_join, canonical_register, get_pubkey, sign_message  # noqa: E402
from diloco.tunnel_allowlist import read_peer_allowlist  # noqa: E402


def _identity(tmp: Path, name: str) -> Path:
    path = tmp / f"{name}.json"
    subprocess.run(
        ["node", str(ROOT / "network/holepunch-sidecar/identity.js"),
         "--cmd", "generate", "--name", name, "--out", str(path)],
        check=True, capture_output=True, text=True,
    )
    return path


class JoinTests(unittest.TestCase):
    def _config(self, tmp: str, *, require_identity: bool = True, transport: str = "http") -> ExperimentConfig:
        return ExperimentConfig(
            run_id="unit-join",
            model=ModelConfig(kind="toy", in_features=8, out_features=4, rank=2),
            train=TrainConfig(H=2, batch_size=4),
            data=DataConfig(num_shards=1, eval_samples=8),
            coordinator=CoordinatorConfig(
                host="127.0.0.1", port=0, min_workers=1, max_workers=2, quorum=1,
                round_timeout_s=10.0, max_rounds=1, checkpoint_dir=tmp,
                token="coordinator-token", idle_timeout_s=30.0,
                require_node_identity=require_identity, transport=transport,
            ),
        )

    def _setup(self, tmp: str, transport: str = "http"):
        cfg = self._config(tmp, transport=transport)
        state, base_hash, meta = initial_adapter_state(cfg, device="cpu")
        coord = Coordinator(cfg, state, base_hash, meta)
        port = coord.start()
        return cfg, coord, port, base_hash

    def _join_payload(self, ident: Path, node_id: str, token: str, cfg_hash: str, base_hash: str) -> Dict[str, Any]:
        pubkey = get_pubkey(ident)
        payload = {
            "type": "diloco-join-v1", "run_id": "unit-join", "node_id": node_id,
            "pubkey": pubkey, "token": token, "config_hash": cfg_hash, "base_hash": base_hash,
        }
        signed = sign_message(ident, canonical_join(payload))
        result = {**payload, "node_sig": signed.get("sig", ""), "sig_alg": signed.get("alg", "")}
        result.pop("type", None)  # coordinator.join() uses the canonical type internally
        return result

    def test_join_admits_worker_and_updates_peer_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, coord, port, base_hash = self._setup(tmp)
            try:
                ident = _identity(Path(tmp), "worker-ok")
                payload = self._join_payload(ident, "node-ok", "token-1", cfg.fingerprint(), base_hash)
                coord.configure_join_token("token-1", max_uses=2, expires_at=time.time() + 60,
                                           config_hash=cfg.fingerprint(), base_hash=base_hash)
                result = coord.join(**payload)
                self.assertTrue(result["ok"])
                self.assertEqual(result["remaining_uses"], 1)
                pubkey = get_pubkey(ident)
                self.assertIn(pubkey, read_peer_allowlist(Path(tmp) / "allowed_peer_pubkeys.txt"))
                # After admission a signed register is accepted.
                sig = sign_message(ident, canonical_register("node-ok", cfg.fingerprint(), base_hash, "unit-join"))
                info = coord.register("node-ok", cfg.fingerprint(), base_hash,
                                      node_pubkey=pubkey, node_sig=sig.get("sig", ""))
                self.assertEqual(info["node_id"], "node-ok")
            finally:
                coord.stop()

    def test_token_single_use_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, coord, port, base_hash = self._setup(tmp)
            try:
                ident = _identity(Path(tmp), "worker-once")
                coord.configure_join_token("once", max_uses=1, expires_at=time.time() + 60,
                                           config_hash=cfg.fingerprint(), base_hash=base_hash)
                coord.join(**self._join_payload(ident, "node-a", "once", cfg.fingerprint(), base_hash))
                with self.assertRaises(ApiError) as caught:
                    coord.join(**self._join_payload(ident, "node-b", "once", cfg.fingerprint(), base_hash))
                self.assertEqual(caught.exception.status, 403)
                self.assertIn("already been used", caught.exception.message)
            finally:
                coord.stop()

    def test_expired_and_wrong_config_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, coord, port, base_hash = self._setup(tmp)
            try:
                ident = _identity(Path(tmp), "worker-expired")
                coord.configure_join_token("expired", max_uses=2, expires_at=1,
                                           config_hash=cfg.fingerprint(), base_hash=base_hash)
                with self.assertRaises(ApiError) as caught:
                    coord.join(**self._join_payload(ident, "node-e", "expired", cfg.fingerprint(), base_hash))
                self.assertEqual(caught.exception.status, 403)
                coord.configure_join_token("badcfg", max_uses=2, expires_at=time.time() + 60,
                                           config_hash=cfg.fingerprint(), base_hash=base_hash)
                with self.assertRaises(ApiError) as caught:
                    coord.join(**self._join_payload(ident, "node-c", "badcfg", "wrong", base_hash))
                self.assertEqual(caught.exception.status, 409)
            finally:
                coord.stop()

    def test_wrong_signature_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, coord, port, base_hash = self._setup(tmp)
            try:
                ident = _identity(Path(tmp), "worker-sig")
                coord.configure_join_token("sig", max_uses=2, expires_at=time.time() + 60,
                                           config_hash=cfg.fingerprint(), base_hash=base_hash)
                payload = self._join_payload(ident, "node-s", "sig", cfg.fingerprint(), base_hash)
                payload["pubkey"] = get_pubkey(_identity(Path(tmp), "attacker"))
                with self.assertRaises(ApiError) as caught:
                    coord.join(**payload)
                self.assertEqual(caught.exception.status, 403)
            finally:
                coord.stop()

    def test_http_join_endpoint_needs_no_coordinator_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, coord, port, base_hash = self._setup(tmp)
            try:
                ident = _identity(Path(tmp), "worker-http")
                coord.configure_join_token("http-tok", max_uses=2, expires_at=time.time() + 60,
                                           config_hash=cfg.fingerprint(), base_hash=base_hash)
                payload = self._join_payload(ident, "node-http", "http-tok", cfg.fingerprint(), base_hash)
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/join",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},  # no X-DiLoCo-Token
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(result["ok"])
                # /v1/invite requires the token
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/invite", timeout=10)
                self.assertEqual(caught.exception.code, 404)
            finally:
                coord.stop()

    def test_invite_endpoint_requires_valid_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, coord, port, base_hash = self._setup(tmp)
            try:
                coord.set_invite({"join_token": "abc", "run_id": "unit-join"})
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/invite?token=abc", timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(data["join_token"], "abc")
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/invite?token=bad", timeout=10)
                self.assertEqual(caught.exception.code, 403)
            finally:
                coord.stop()

    def test_grpc_join_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, coord, port, base_hash = self._setup(tmp, transport="grpc")
            try:
                from diloco.grpc_transport import GrpcWorkerClient

                ident = _identity(Path(tmp), "worker-grpc")
                coord.configure_join_token("grpc-tok", max_uses=2, expires_at=time.time() + 60,
                                           config_hash=cfg.fingerprint(), base_hash=base_hash)
                payload = self._join_payload(ident, "node-grpc", "grpc-tok", cfg.fingerprint(), base_hash)
                client = GrpcWorkerClient(f"127.0.0.1:{port}", token="coordinator-token")
                result = client.join(**{k: v for k, v in payload.items() if k in (
                    "node_id", "pubkey", "node_sig", "token", "config_hash", "base_hash", "sig_alg", "run_id")})
                self.assertTrue(result.get("ok"))
                self.assertIn(get_pubkey(ident), read_peer_allowlist(Path(tmp) / "allowed_peer_pubkeys.txt"))
                client.channel.close()
            finally:
                coord.stop()


if __name__ == "__main__":
    unittest.main()
