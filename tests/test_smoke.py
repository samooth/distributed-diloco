from __future__ import annotations

import subprocess
import sys
import time
import tempfile
import threading
import unittest
from dataclasses import replace

import torch
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diloco.backend import build_backend, initial_adapter_state
from diloco.checkpoint import CheckpointStore
from diloco.config import (
    CoordinatorConfig,
    DataConfig,
    ExperimentConfig,
    ModelConfig,
    TrainConfig,
)
from diloco.coordinator import ApiError, Coordinator
from diloco.protocol import pack_message
from diloco.tensor_utils import hash_state
from diloco.worker import CoordinatorError, Worker


class LocalhostSmokeTests(unittest.TestCase):
    def test_late_join_registration_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            def make(allow: bool) -> tuple[ExperimentConfig, Coordinator]:
                cfg = ExperimentConfig(
                    run_id="unit-late-join",
                    model=ModelConfig(kind="toy", in_features=8, out_features=4, rank=2),
                    train=TrainConfig(H=2, batch_size=4),
                    data=DataConfig(num_shards=2, eval_samples=8),
                    coordinator=CoordinatorConfig(
                        min_workers=2,
                        max_workers=3,
                        quorum=2,
                        max_rounds=2,
                        checkpoint_dir=tmp,
                        token="unit-token",
                        allow_late_join=allow,
                    ),
                )
                state, base_hash, meta = initial_adapter_state(cfg, device="cpu")
                coordinator = Coordinator(cfg, state, base_hash, meta)
                return cfg, coordinator

            cfg, coordinator = make(True)
            coordinator.register("node-0", cfg.fingerprint(), coordinator.base_hash)
            coordinator.register("node-1", cfg.fingerprint(), coordinator.base_hash)
            with coordinator.cond:
                coordinator.registration_open = False
            info = coordinator.register("node-2", cfg.fingerprint(), coordinator.base_hash)
            self.assertEqual(info["round"], 0)
            self.assertIn("node-2", info["registered"])

            cfg, coordinator = make(False)
            coordinator.register("node-0", cfg.fingerprint(), coordinator.base_hash)
            coordinator.register("node-1", cfg.fingerprint(), coordinator.base_hash)
            with coordinator.cond:
                coordinator.registration_open = False
            with self.assertRaises(ApiError) as caught:
                coordinator.register("node-2", cfg.fingerprint(), coordinator.base_hash)
            self.assertEqual(caught.exception.status, 409)

    def test_worker_retries_stale_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ExperimentConfig(
                run_id="unit-stale-retry",
                model=ModelConfig(kind="toy", in_features=8, out_features=4, rank=2),
                train=TrainConfig(H=2, batch_size=4, seed=7),
                data=DataConfig(num_shards=1, eval_samples=8),
                coordinator=CoordinatorConfig(
                    min_workers=1,
                    max_workers=1,
                    quorum=1,
                    max_rounds=2,
                    checkpoint_dir=tmp,
                    token="",
                    idle_timeout_s=0,
                ),
            )
            state, base_hash, adapter_meta = initial_adapter_state(config, device="cpu")
            backend = build_backend(config, shard_id=0, device="cpu")
            worker = Worker(
                config,
                "node-0",
                "http://127.0.0.1:1",
                backend,
                log_path=str(Path(tmp) / "worker.jsonl"),
            )
            calls: List[tuple[str, str]] = []
            submit_count = 0
            state_calls = 0

            def blob(round_index: int, done: bool) -> bytes:
                meta = {
                    "round": round_index,
                    "global_step": round_index * config.train.H,
                    "H": config.train.H,
                    "max_rounds": config.coordinator.max_rounds,
                    "done": done,
                    "state_hash": hash_state(state),
                    "base_hash": base_hash,
                    "config_hash": config.fingerprint(),
                    "adapter_meta": adapter_meta,
                }
                return pack_message(meta, state)

            def meta_json(round_index: int, done: bool) -> bytes:
                meta = {
                    "round": round_index,
                    "global_step": round_index * config.train.H,
                    "H": config.train.H,
                    "max_rounds": config.coordinator.max_rounds,
                    "done": done,
                    "state_hash": hash_state(state),
                    "base_hash": base_hash,
                    "config_hash": config.fingerprint(),
                    "adapter_meta": adapter_meta,
                }
                return json.dumps(meta).encode("utf-8")

            def fake_request(
                method: str,
                path: str,
                body: bytes | None = None,
                content_type: str = "application/json",
                timeout: float | None = None,
                extra_headers: Dict[str, str] | None = None,
                allow_partial: bool = False,
            ) -> tuple[int, bytes, str, Dict[str, str]]:
                nonlocal submit_count, state_calls
                calls.append((method, path))
                if path.startswith("/v1/state_meta"):
                    # first fetch: round 0; after stale retry worker fetches state again -> round 1
                    round_index = 0 if state_calls == 0 else 1
                    return 200, meta_json(round_index, False), "application/json", {}
                if path.startswith("/v1/state"):
                    state_calls += 1
                    round_index = 0 if state_calls == 1 else 1
                    return 200, blob(round_index, False), "application/octet-stream", {}
                if path.startswith("/v1/update"):
                    submit_count += 1
                    if submit_count == 1:
                        raise CoordinatorError(409, "stale round 0; current round is 1")
                    return 200, blob(2, True), "application/octet-stream", {}
                if path.startswith("/v1/leave"):
                    return 200, b'{"ok":true,"removed":true}', "application/json", {}
                raise AssertionError(f"unexpected request: {method} {path}")

            worker._request = fake_request  # type: ignore[method-assign]
            try:
                summary = worker._run_registered({"registered": []})
            finally:
                worker.close()

            self.assertEqual(submit_count, 2)
            self.assertEqual(summary["rounds_done"], 1)
            self.assertEqual(summary["final_round"], 2)
            log = (Path(tmp) / "worker.jsonl").read_text()
            self.assertIn("stale_round_retry", log)


    def test_evicts_idle_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ExperimentConfig(
                run_id="unit-evict",
                model=ModelConfig(kind="toy", in_features=4, out_features=2, rank=2),
                train=TrainConfig(H=2, batch_size=4),
                data=DataConfig(num_shards=1, eval_samples=4),
                coordinator=CoordinatorConfig(
                    min_workers=1,
                    max_workers=2,
                    quorum=1,
                    max_rounds=1,
                    checkpoint_dir=tmp,
                    token="unit-token",
                    idle_timeout_s=0.01,
                ),
            )
            state, base_hash, meta = initial_adapter_state(config, device="cpu")
            coordinator = Coordinator(config, state, base_hash, meta)
            coordinator.register("node-0", config.fingerprint(), coordinator.base_hash)
            time.sleep(0.03)
            with self.assertRaises(ApiError) as caught:
                coordinator.heartbeat("node-0")
            self.assertEqual(caught.exception.status, 403)
            self.assertNotIn("node-0", coordinator.registered)

    def test_leave_removes_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ExperimentConfig(
                run_id="unit-leave",
                model=ModelConfig(kind="toy", in_features=4, out_features=2, rank=2),
                train=TrainConfig(H=2, batch_size=4),
                data=DataConfig(num_shards=1, eval_samples=4),
                coordinator=CoordinatorConfig(
                    min_workers=1,
                    max_workers=2,
                    quorum=1,
                    max_rounds=1,
                    checkpoint_dir=tmp,
                    token="unit-token",
                    idle_timeout_s=30.0,
                ),
            )
            state, base_hash, meta = initial_adapter_state(config, device="cpu")
            coordinator = Coordinator(config, state, base_hash, meta)
            coordinator.register("node-0", config.fingerprint(), coordinator.base_hash)
            result = coordinator.leave("node-0")
            self.assertTrue(result["removed"])
            with self.assertRaises(ApiError):
                coordinator.heartbeat("node-0")

    def test_checkpoint_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            state = {"a": torch.tensor([1.0])}
            for round_index in range(1, 8):
                store.save(round_index, round_index, state, state, "cfg", "base")
            store.prune(2)
            rounds = [p.name for p in Path(tmp).glob("checkpoint-*")]
            self.assertEqual(sorted(rounds), ["checkpoint-000006", "checkpoint-000007"])
            self.assertEqual(store.latest_round(), 7)



    def test_checkpoint_syncs_to_object_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            from diloco.object_store import LocalObjectStore

            store_root = Path(tmp) / "store"
            store = CheckpointStore(tmp, object_store=LocalObjectStore(store_root))
            state = {"a": torch.tensor([1.0, 2.0])}
            store.save(1, 10, state, state, "cfg", "base")
            store.save(2, 20, state, state, "cfg", "base")
            self.assertEqual(sorted(store.object_store.list("checkpoints")), ["checkpoints/checkpoint-000001/adapter.safetensors", "checkpoints/checkpoint-000001/meta.json", "checkpoints/checkpoint-000001/outer.safetensors", "checkpoints/checkpoint-000002/adapter.safetensors", "checkpoints/checkpoint-000002/meta.json", "checkpoints/checkpoint-000002/outer.safetensors"])
            store.prune(1)
            self.assertEqual(store.object_store.list("checkpoints/checkpoint-000001"), [])
            self.assertEqual(store.latest_round(), 2)

    def test_evicted_worker_can_rejoin_with_late_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ExperimentConfig(
                run_id="unit-rejoin",
                model=ModelConfig(kind="toy", in_features=4, out_features=2, rank=2),
                train=TrainConfig(H=2, batch_size=4),
                data=DataConfig(num_shards=1, eval_samples=4),
                coordinator=CoordinatorConfig(
                    min_workers=1,
                    max_workers=2,
                    quorum=1,
                    max_rounds=1,
                    checkpoint_dir=tmp,
                    token="unit-token",
                    idle_timeout_s=0.01,
                    allow_late_join=True,
                ),
            )
            state, base_hash, meta = initial_adapter_state(config, device="cpu")
            coordinator = Coordinator(config, state, base_hash, meta)
            coordinator.register("node-0", config.fingerprint(), coordinator.base_hash)
            time.sleep(0.03)
            with self.assertRaises(ApiError):
                coordinator.heartbeat("node-0")
            info = coordinator.register("node-0", config.fingerprint(), coordinator.base_hash)
            self.assertEqual(info["node_id"], "node-0")
            self.assertIn("node-0", info["registered"])

    def test_evicted_known_worker_can_rejoin_without_late_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ExperimentConfig(
                run_id="unit-rejoin-no-late",
                model=ModelConfig(kind="toy", in_features=4, out_features=2, rank=2),
                train=TrainConfig(H=2, batch_size=4),
                data=DataConfig(num_shards=1, eval_samples=4),
                coordinator=CoordinatorConfig(
                    min_workers=1,
                    max_workers=2,
                    quorum=1,
                    max_rounds=1,
                    checkpoint_dir=tmp,
                    token="unit-token",
                    idle_timeout_s=0.01,
                    allow_late_join=False,
                ),
            )
            state, base_hash, meta = initial_adapter_state(config, device="cpu")
            coordinator = Coordinator(config, state, base_hash, meta)
            coordinator.register("node-0", config.fingerprint(), coordinator.base_hash)
            time.sleep(0.03)
            with self.assertRaises(ApiError):
                coordinator.heartbeat("node-0")
            info = coordinator.register("node-0", config.fingerprint(), coordinator.base_hash)
            self.assertEqual(info["node_id"], "node-0")
            self.assertIn("node-0", info["registered"])

    def test_single_worker_int8_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ExperimentConfig(
                run_id="unit-int8",
                model=ModelConfig(kind="toy", in_features=16, out_features=8, rank=4, alpha=4.0),
                train=TrainConfig(
                    H=5,
                    inner_lr=0.05,
                    batch_size=8,
                    seed=42,
                    delta_compression="int8",
                ),
                data=DataConfig(num_shards=1, eval_samples=32, eval_every=1),
                coordinator=CoordinatorConfig(
                    host="127.0.0.1",
                    port=0,
                    min_workers=1,
                    max_workers=1,
                    quorum=1,
                    round_timeout_s=15.0,
                    max_rounds=1,
                    checkpoint_dir=tmp,
                    checkpoint_every=1,
                    token="unit-token",
                    idle_timeout_s=30.0,
                ),
            )
            state, base_hash, adapter_meta = initial_adapter_state(config, device="cpu")
            coordinator = Coordinator(config, state, base_hash, adapter_meta)
            port = coordinator.start()
            worker = Worker(
                config,
                "node-0",
                f"http://127.0.0.1:{port}",
                build_backend(config, shard_id=0, device="cpu"),
                log_path=str(Path(tmp) / "node-0.jsonl"),
            )
            try:
                summary = worker.run()
                self.assertEqual(summary["rounds_done"], 1)
                self.assertLess(summary["final_eval_loss"], summary["initial_eval_loss"])
                self.assertTrue(coordinator.status()["finished"])
                self.assertIsNone(coordinator.status()["failure"])
                checkpoint = CheckpointStore(tmp).load_latest()
                self.assertIsNotNone(checkpoint)
                assert checkpoint is not None
                self.assertEqual(checkpoint.round, 1)
            finally:
                coordinator.stop()
                worker.close()

    def test_worker_recovers_after_mid_round_eviction(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ExperimentConfig(
                run_id="unit-churn",
                model=ModelConfig(kind="toy", in_features=8, out_features=4, rank=2, alpha=4.0),
                train=TrainConfig(H=4, inner_lr=0.05, batch_size=4, seed=7),
                data=DataConfig(num_shards=1, eval_samples=16, eval_every=1),
                coordinator=CoordinatorConfig(
                    host="127.0.0.1",
                    port=0,
                    min_workers=1,
                    max_workers=1,
                    quorum=1,
                    round_timeout_s=30.0,
                    max_rounds=2,
                    checkpoint_dir=tmp,
                    checkpoint_every=1,
                    token="unit-token",
                    idle_timeout_s=30.0,
                    allow_late_join=False,
                ),
            )
            state, base_hash, adapter_meta = initial_adapter_state(config, device="cpu")
            coordinator = Coordinator(config, state, base_hash, adapter_meta)
            port = coordinator.start()
            backend = build_backend(config, shard_id=0, device="cpu")
            original_train = backend.train_steps

            def slow_train(steps: int, round_index: int = 0) -> dict:
                time.sleep(0.4)
                return original_train(steps, round_index=round_index)

            backend.train_steps = slow_train  # type: ignore[method-assign]
            worker = Worker(
                config,
                "node-0",
                f"http://127.0.0.1:{port}",
                backend,
                log_path=str(Path(tmp) / "node-0.jsonl"),
            )
            result: dict[str, Any] = {}

            def runner() -> None:
                result["summary"] = worker.run()

            thread = threading.Thread(target=runner, daemon=True)
            thread.start()
            time.sleep(0.15)
            coordinator.leave("node-0")
            thread.join(timeout=30.0)
            self.assertFalse(thread.is_alive())
            try:
                coordinator.stop()
            except Exception:
                pass
            worker.close()
            summary = result.get("summary")
            self.assertIsNotNone(summary)
            assert summary is not None
            self.assertEqual(summary["rounds_done"], 2)
            self.assertTrue(coordinator.status()["finished"])
            self.assertIsNone(coordinator.status()["failure"])

    def _make_tls_certs(self, root: Path) -> dict[str, str]:
        root.mkdir(parents=True, exist_ok=True)
        ca_key = root / "ca.key"
        ca_cert = root / "ca.crt"
        server_key = root / "server.key"
        server_csr = root / "server.csr"
        server_cert = root / "server.crt"
        client_key = root / "client.key"
        client_csr = root / "client.csr"
        client_cert = root / "client.crt"
        ext = root / "server.ext"
        ext.write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n")
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(ca_key), "-out", str(ca_cert), "-subj", "/CN=diloco-test-ca", "-days", "2"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**__import__("os").environ, "OPENSSL_CONF": "/etc/ssl/openssl.cnf"},
        )
        subprocess.run(
            ["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(server_key), "-out", str(server_csr), "-subj", "/CN=localhost"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**__import__("os").environ, "OPENSSL_CONF": "/etc/ssl/openssl.cnf"},
        )
        subprocess.run(
            ["openssl", "x509", "-req", "-in", str(server_csr), "-CA", str(ca_cert), "-CAkey", str(ca_key), "-CAcreateserial", "-out", str(server_cert), "-days", "2", "-extfile", str(ext)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**__import__("os").environ, "OPENSSL_CONF": "/etc/ssl/openssl.cnf"},
        )
        subprocess.run(
            ["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(client_key), "-out", str(client_csr), "-subj", "/CN=diloco-client"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**__import__("os").environ, "OPENSSL_CONF": "/etc/ssl/openssl.cnf"},
        )
        subprocess.run(
            ["openssl", "x509", "-req", "-in", str(client_csr), "-CA", str(ca_cert), "-CAkey", str(ca_key), "-CAcreateserial", "-out", str(client_cert), "-days", "2"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**__import__("os").environ, "OPENSSL_CONF": "/etc/ssl/openssl.cnf"},
        )
        return {
            "ca": str(ca_cert),
            "server_cert": str(server_cert),
            "server_key": str(server_key),
            "client_cert": str(client_cert),
            "client_key": str(client_key),
        }

    def test_single_worker_grpc_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ExperimentConfig(
                run_id="unit-grpc",
                model=ModelConfig(kind="toy", in_features=16, out_features=8, rank=4, alpha=4.0),
                train=TrainConfig(H=5, inner_lr=0.05, batch_size=8, seed=42),
                data=DataConfig(num_shards=1, eval_samples=32, eval_every=1),
                coordinator=CoordinatorConfig(
                    host="127.0.0.1",
                    port=0,
                    min_workers=1,
                    max_workers=1,
                    quorum=1,
                    round_timeout_s=15.0,
                    max_rounds=1,
                    checkpoint_dir=tmp,
                    checkpoint_every=1,
                    token="unit-token",
                    idle_timeout_s=30.0,
                    transport="grpc",
                ),
            )
            state, base_hash, adapter_meta = initial_adapter_state(config, device="cpu")
            coordinator = Coordinator(config, state, base_hash, adapter_meta)
            port = coordinator.start()
            worker = Worker(
                config,
                "node-0",
                f"grpc://127.0.0.1:{port}",
                build_backend(config, shard_id=0, device="cpu"),
                log_path=str(Path(tmp) / "node-0.jsonl"),
                grpc_target=f"127.0.0.1:{port}",
            )
            try:
                summary = worker.run()
                self.assertEqual(summary["rounds_done"], 1)
                self.assertLess(summary["final_eval_loss"], summary["initial_eval_loss"])
                self.assertTrue(coordinator.status()["finished"])
                self.assertIsNone(coordinator.status()["failure"])
                checkpoint = CheckpointStore(tmp).load_latest()
                self.assertIsNotNone(checkpoint)
                assert checkpoint is not None
                self.assertEqual(checkpoint.round, 1)
            finally:
                coordinator.stop()
                worker.close()

    def test_single_worker_grpc_tls_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            certs = self._make_tls_certs(Path(tmp) / "certs")
            base = ExperimentConfig(
                run_id="unit-grpc-tls",
                model=ModelConfig(kind="toy", in_features=16, out_features=8, rank=4, alpha=4.0),
                train=TrainConfig(H=5, inner_lr=0.05, batch_size=8, seed=42),
                data=DataConfig(num_shards=1, eval_samples=32, eval_every=1),
                coordinator=CoordinatorConfig(
                    host="127.0.0.1",
                    port=0,
                    min_workers=1,
                    max_workers=1,
                    quorum=1,
                    round_timeout_s=15.0,
                    max_rounds=1,
                    checkpoint_dir=tmp,
                    checkpoint_every=1,
                    token="unit-token",
                    idle_timeout_s=30.0,
                    transport="grpc",
                    tls_cert=certs["server_cert"],
                    tls_key=certs["server_key"],
                    tls_client_ca=certs["ca"],
                    tls_require_client_cert=True,
                ),
            )
            state, base_hash, adapter_meta = initial_adapter_state(base, device="cpu")
            coordinator = Coordinator(base, state, base_hash, adapter_meta)
            port = coordinator.start()
            worker_config = replace(
                base,
                coordinator=replace(
                    base.coordinator,
                    tls_cert=certs["client_cert"],
                    tls_key=certs["client_key"],
                    tls_client_ca=certs["ca"],
                    tls_verify=True,
                ),
            )
            worker = Worker(
                worker_config,
                "node-0",
                f"grpc://127.0.0.1:{port}",
                build_backend(worker_config, shard_id=0, device="cpu"),
                log_path=str(Path(tmp) / "node-0.jsonl"),
                grpc_target=f"127.0.0.1:{port}",
            )
            try:
                summary = worker.run()
                self.assertEqual(summary["rounds_done"], 1)
                self.assertLess(summary["final_eval_loss"], summary["initial_eval_loss"])
                self.assertTrue(coordinator.status()["finished"])
                self.assertIsNone(coordinator.status()["failure"])
            finally:
                coordinator.stop()
                worker.close()

    def test_two_islands_complete_four_rounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ExperimentConfig(
                run_id="unit-smoke",
                model=ModelConfig(kind="toy", in_features=16, out_features=8, rank=4, alpha=4.0),
                train=TrainConfig(H=20, inner_lr=0.05, batch_size=16, seed=42),
                data=DataConfig(num_shards=2, eval_samples=64, eval_every=1),
                coordinator=CoordinatorConfig(
                    host="127.0.0.1",
                    port=0,
                    min_workers=2,
                    max_workers=4,
                    quorum=2,
                    round_timeout_s=15.0,
                    max_rounds=4,
                    checkpoint_dir=tmp,
                    checkpoint_every=1,
                    token="unit-token",
                    idle_timeout_s=30.0,
                ),
            )
            state, base_hash, adapter_meta = initial_adapter_state(config, device="cpu")
            coordinator = Coordinator(config, state, base_hash, adapter_meta)
            port = coordinator.start()
            url = f"http://127.0.0.1:{port}"

            summaries: List[Dict[str, Any]] = []
            errors: List[str] = []
            lock = threading.Lock()
            workers: List[Worker] = []

            def run(index: int) -> None:
                worker = None
                try:
                    backend = build_backend(config, shard_id=index, device="cpu")
                    worker = Worker(
                        config,
                        f"node-{index}",
                        url,
                        backend,
                        log_path=str(Path(tmp) / f"node-{index}.jsonl"),
                    )
                    with lock:
                        workers.append(worker)
                    result = worker.run()
                    with lock:
                        summaries.append(result)
                except Exception as exc:  # noqa: BLE001
                    with lock:
                        errors.append(f"node-{index}: {exc}")
                finally:
                    if worker is not None:
                        worker.close()

            threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(2)]
            try:
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
                self.assertEqual(errors, [])
                self.assertFalse([thread for thread in threads if thread.is_alive()])
                status = coordinator.status()
                self.assertTrue(status["finished"])
                self.assertIsNone(status["failure"])
                self.assertEqual(status["round"], 4)
                self.assertEqual(status["last_result"]["nodes"], ["node-0", "node-1"])
                self.assertEqual(len(summaries), 2)
                for summary in summaries:
                    self.assertEqual(summary["rounds_done"], 4)
                    self.assertLess(
                        summary["final_eval_loss"],
                        summary["initial_eval_loss"],
                    )
                checkpoint = CheckpointStore(tmp).load_latest()
                self.assertIsNotNone(checkpoint)
                assert checkpoint is not None
                self.assertEqual(checkpoint.round, 4)
                self.assertEqual(checkpoint.global_step, 80)
                self.assertEqual(checkpoint.meta["state_hash"], status["state_hash"])
            finally:
                coordinator.stop()
                for worker in workers:
                    worker.close()


if __name__ == "__main__":
    unittest.main()
