from __future__ import annotations

import sys
import tempfile
import threading
import unittest
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

            def fake_request(
                method: str,
                path: str,
                body: bytes | None = None,
                content_type: str = "application/json",
                timeout: float | None = None,
            ) -> tuple[int, bytes, str]:
                nonlocal submit_count
                calls.append((method, path))
                if path.startswith("/v1/state"):
                    round_index = 0 if sum(c[0] == "GET" for c in calls) == 1 else 1
                    return 200, blob(round_index, False), "application/octet-stream"
                if path.startswith("/v1/update"):
                    submit_count += 1
                    if submit_count == 1:
                        raise CoordinatorError(409, "stale round 0; current round is 1")
                    return 200, blob(2, True), "application/octet-stream"
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
