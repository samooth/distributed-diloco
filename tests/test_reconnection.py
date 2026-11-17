"""P2.3: reconnection / re-registration E2E hardening tests."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diloco.backend import build_backend, initial_adapter_state  # noqa: E402
from diloco.config import (  # noqa: E402
    CoordinatorConfig,
    DataConfig,
    ExperimentConfig,
    ModelConfig,
    TrainConfig,
)
from diloco.coordinator import Coordinator  # noqa: E402
from diloco.worker import Worker  # noqa: E402


def _config(tmp: str, *, rounds: int, port: int = 0, allow_late_join: bool = True) -> ExperimentConfig:
    return ExperimentConfig(
        run_id="unit-reconnection",
        model=ModelConfig(kind="toy", in_features=8, out_features=4, rank=2, alpha=4.0),
        train=TrainConfig(H=2, inner_lr=0.05, batch_size=4, seed=13),
        data=DataConfig(num_shards=1, eval_samples=8, eval_every=1),
        coordinator=CoordinatorConfig(
            host="127.0.0.1",
            port=port,
            min_workers=1,
            max_workers=1,
            quorum=1,
            round_timeout_s=10.0,
            max_rounds=rounds,
            checkpoint_dir=tmp,
            checkpoint_every=1,
            token="reconnect-token",
            idle_timeout_s=30.0,
            registration_timeout_s=30.0,
            allow_late_join=allow_late_join,
        ),
    )


class ReconnectionTests(unittest.TestCase):
    def test_heartbeat_reregisters_after_lost_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp, rounds=3)
            state, base_hash, meta = initial_adapter_state(cfg, device="cpu")
            coordinator = Coordinator(cfg, state, base_hash, meta)
            port = coordinator.start()
            worker = Worker(
                cfg,
                "node-reconnect",
                f"http://127.0.0.1:{port}",
                build_backend(cfg, shard_id=0, device="cpu"),
                log_path=str(Path(tmp) / "node.jsonl"),
            )
            try:
                worker.register()
                worker._heartbeat_interval = 0.05
                worker._start_heartbeat()
                self.assertIn("node-reconnect", coordinator.status()["registered"])
                # Simulate lost session on the coordinator side.
                coordinator.leave("node-reconnect")
                self.assertNotIn("node-reconnect", coordinator.status()["registered"])
                deadline = time.time() + 3.0
                while time.time() < deadline and "node-reconnect" not in coordinator.status()["registered"]:
                    time.sleep(0.05)
                self.assertIn("node-reconnect", coordinator.status()["registered"])
            finally:
                worker._stop_heartbeat()
                worker.close()
                coordinator.stop()

    def test_worker_recovers_after_coordinator_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp, rounds=2)
            state, base_hash, meta = initial_adapter_state(cfg, device="cpu")
            coordinator = Coordinator(cfg, state, base_hash, meta)
            port = coordinator.start()
            cfg_restarted = _config(tmp, rounds=2, port=port)
            restarted = Coordinator(cfg_restarted, state, base_hash, meta)
            url = f"http://127.0.0.1:{port}"
            summaries: Dict[str, Any] = {}
            errors: list[str] = []
            restart_done = threading.Event()

            def run() -> None:
                worker = None
                try:
                    backend = build_backend(cfg, shard_id=0, device="cpu")
                    original_train = backend.train_steps

                    def slow_train(steps: int, round_index: int = 0) -> dict:
                        time.sleep(0.6)  # keep the worker mid-round while we restart
                        return original_train(steps, round_index=round_index)

                    backend.train_steps = slow_train  # type: ignore[method-assign]
                    worker = Worker(
                        cfg,
                        "node-restart",
                        url,
                        backend,
                        log_path=str(Path(tmp) / "node-restart.jsonl"),
                    )
                    summaries["summary"] = worker.run()
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))
                finally:
                    if worker is not None:
                        worker.close()

            thread = threading.Thread(target=run, daemon=True)
            try:
                thread.start()
                # Wait until round 0 is in flight, then restart the coordinator
                # on the same port (SO_REUSEADDR is enabled in the server).
                deadline = time.time() + 10
                while time.time() < deadline and coordinator.status()["round"] < 1:
                    time.sleep(0.05)
                coordinator.stop()
                time.sleep(0.3)
                restarted.start()
                restart_done.set()
                thread.join(timeout=60)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                summary = summaries.get("summary")
                self.assertIsNotNone(summary)
                assert summary is not None
                self.assertIsNone(summary.get("failure"))
                st = restarted.status()
                self.assertTrue(st["finished"])
                self.assertIsNone(st["failure"])
            finally:
                if not restart_done.is_set():
                    try:
                        coordinator.stop()
                    except Exception:
                        pass
                try:
                    restarted.stop()
                except Exception:
                    pass

    def test_ambiguous_submit_recovers_without_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp, rounds=2)
            state, base_hash, meta = initial_adapter_state(cfg, device="cpu")
            coordinator = Coordinator(cfg, state, base_hash, meta)
            port = coordinator.start()
            worker = Worker(
                cfg,
                "node-duplicate",
                f"http://127.0.0.1:{port}",
                build_backend(cfg, shard_id=0, device="cpu"),
                log_path=str(Path(tmp) / "node-duplicate.jsonl"),
            )
            try:
                original = worker._request
                duplicate: Dict[str, Any] = {"fired": False}

                def request_with_duplicate(*a: Any, **kw: Any) -> Any:
                    if a and len(a) > 1 and str(a[1]).startswith("/v1/update") and not duplicate["fired"]:
                        duplicate["fired"] = True
                        # First attempt: let it reach the server, then report a
                        # transient error so the worker retries the same update.
                        result = original(*a, **kw)
                        raise __import__("diloco.worker", fromlist=["CoordinatorError"]).CoordinatorError(
                            0, "simulated ambiguous submit"
                        )
                    return original(*a, **kw)

                worker._request = request_with_duplicate  # type: ignore[method-assign]
                summary = worker.run()
                self.assertTrue(duplicate["fired"])
                self.assertIsNone(summary.get("failure"))
                self.assertEqual(int(summary["final_round"]), 2, summary)
                st = coordinator.status()
                self.assertTrue(st["finished"])
                self.assertEqual(st["round"], 2)
                self.assertIsNone(st["failure"])
            finally:
                try:
                    worker.leave()
                except Exception:
                    pass
                worker.close()
                coordinator.stop()


if __name__ == "__main__":
    unittest.main()
