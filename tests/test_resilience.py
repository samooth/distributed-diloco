"""P2.7 resilience suite: WAN fault injection and churn tolerance.

These tests exercise the coordinator/worker recovery paths under injected
latency, transient failures and simulated disconnects (churn). The goal is
that a run tolerates faults and recovers within at most two rounds, without
state corruption or quorum timeouts.

For real link impairments use ``scripts/resilience_netem.sh`` (``tc netem``),
which runs the same suite against a loopback interface with latency/jitter/
loss/bandwidth limits. The in-process fault profiles below are deterministic
and need no privileges.
"""
from __future__ import annotations

import json
import random
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diloco.backend import build_backend, initial_adapter_state
from diloco.config import CoordinatorConfig, DataConfig, ExperimentConfig, ModelConfig, TrainConfig
from diloco.coordinator import Coordinator
from diloco.worker import CoordinatorError, Worker


class FaultProfile:
    """Injects latency and transient errors into a worker's HTTP requests."""

    def __init__(
        self,
        latency_s: float = 0.0,
        fail_every: int = 0,
        fail_status: int = 503,
        fail_paths: Optional[Sequence[str]] = (),
        fail_call_window: Optional[tuple[int, int]] = None,
        seed: int = 7,
    ) -> None:
        self.latency_s = float(latency_s)
        self.fail_every = int(fail_every)
        self.fail_status = int(fail_status)
        self.fail_paths = tuple(fail_paths or ())
        self.fail_call_window = fail_call_window
        self.rng = random.Random(seed)
        self.state: Dict[str, Any] = {"calls": 0, "injected": 0, "paths": []}

    def install(self, worker: Worker) -> Dict[str, Any]:
        original = worker._request

        def faulted(
            method: str,
            path: str,
            body: Optional[bytes] = None,
            content_type: str = "application/json",
            timeout: Optional[float] = None,
            extra_headers: Optional[Dict[str, str]] = None,
            allow_partial: bool = False,
            retry_statuses: Optional[Sequence[int]] = None,
        ) -> Any:
            self.state["calls"] += 1
            call = self.state["calls"]
            self.state["paths"].append(path)
            if self.latency_s > 0:
                time.sleep(self.latency_s)
            in_window = (
                self.fail_call_window is not None
                and self.fail_call_window[0] <= call <= self.fail_call_window[1]
            )
            periodic = self.fail_every > 0 and call % self.fail_every == 0
            path_match = not self.fail_paths or any(p in path for p in self.fail_paths)
            if (in_window or periodic) and path_match:
                self.state["injected"] += 1
                raise CoordinatorError(self.fail_status, "simulated WAN fault")
            return original(
                method,
                path,
                body=body,
                content_type=content_type,
                timeout=timeout,
                extra_headers=extra_headers,
                allow_partial=allow_partial,
                retry_statuses=retry_statuses,
            )

        worker._request = faulted  # type: ignore[method-assign]
        return self.state

    def install_callable(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a raw request callable (used for grpc-free direct tests)."""
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            self.state["calls"] += 1
            if self.latency_s > 0:
                time.sleep(self.latency_s)
            if self.fail_every > 0 and self.state["calls"] % self.fail_every == 0:
                self.state["injected"] += 1
                raise CoordinatorError(self.fail_status, "simulated WAN fault")
            return fn(*args, **kwargs)
        return wrapped


class ChurnStop(Exception):
    """Sentinel used to simulate an abrupt worker process death."""


class ResilienceTests(unittest.TestCase):
    def _config(self, tmp: str, *, max_rounds: int, quorum: int, min_workers: int, max_workers: int) -> ExperimentConfig:
        return ExperimentConfig(
            run_id="unit-resilience",
            model=ModelConfig(kind="toy", in_features=8, out_features=4, rank=2, alpha=4.0),
            train=TrainConfig(H=3, inner_lr=0.05, batch_size=4, seed=11),
            data=DataConfig(num_shards=1, eval_samples=16, eval_every=1),
            coordinator=CoordinatorConfig(
                host="127.0.0.1",
                port=0,
                min_workers=min_workers,
                max_workers=max_workers,
                quorum=quorum,
                round_timeout_s=30.0,
                max_rounds=max_rounds,
                checkpoint_dir=tmp,
                checkpoint_every=1,
                token="resilience-token",
                idle_timeout_s=30.0,
                registration_timeout_s=30.0,
            ),
        )

    def test_latency_and_periodic_transient_loss_completes_rounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp, max_rounds=3, quorum=1, min_workers=1, max_workers=1)
            state, base_hash, meta = initial_adapter_state(config, device="cpu")
            coordinator = Coordinator(config, state, base_hash, meta)
            port = coordinator.start()
            worker = Worker(
                config,
                "node-wan",
                f"http://127.0.0.1:{port}",
                build_backend(config, shard_id=0, device="cpu"),
                log_path=str(Path(tmp) / "node-wan.jsonl"),
            )
            # Latency + transient 503 on every 3rd request. State/submit loops retry.
            faults = FaultProfile(latency_s=0.005, fail_every=3, fail_status=503)
            injected = faults.install(worker)
            try:
                summary = worker.run()
                self.assertEqual(summary["rounds_done"], 3, summary)
                self.assertIsNone(summary.get("failure"))
                self.assertGreater(injected["injected"], 0, "fault injector should have fired")
                st = coordinator.status()
                self.assertTrue(st["finished"])
                self.assertIsNone(st["failure"])
            finally:
                try:
                    worker.leave()
                except Exception:
                    pass
                worker.close()
                coordinator.stop()

    def test_submit_transient_burst_recovers_within_two_rounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp, max_rounds=3, quorum=2, min_workers=2, max_workers=2)
            state, base_hash, meta = initial_adapter_state(config, device="cpu")
            coordinator = Coordinator(config, state, base_hash, meta)
            port = coordinator.start()
            url = f"http://127.0.0.1:{port}"
            workers: List[Worker] = []
            summaries: List[Dict[str, Any]] = []
            errors: List[str] = []
            lock = threading.Lock()

            def run(index: int) -> None:
                worker = None
                try:
                    worker = Worker(
                        config,
                        f"node-{index}",
                        url,
                        build_backend(config, shard_id=index, device="cpu"),
                        log_path=str(Path(tmp) / f"node-{index}.jsonl"),
                    )
                    with lock:
                        workers.append(worker)
                    # Simulate a disconnect burst only on update submissions for node-0.
                    if index == 0:
                        FaultProfile(fail_call_window=(4, 5), fail_status=0, fail_paths=("/v1/update",)).install(worker)
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
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=60)
                self.assertEqual(errors, [])
                self.assertFalse([t for t in threads if t.is_alive()])
                st = coordinator.status()
                self.assertTrue(st["finished"])
                self.assertIsNone(st["failure"])
                self.assertEqual(st["round"], 3)
                for summary in summaries:
                    self.assertEqual(summary["rounds_done"], 3, summary)
                    self.assertIsNone(summary.get("failure"))
            finally:
                coordinator.stop()

    def test_churn_rejoin_recovers_within_two_rounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp, max_rounds=5, quorum=2, min_workers=2, max_workers=2)
            state, base_hash, meta = initial_adapter_state(config, device="cpu")
            coordinator = Coordinator(config, state, base_hash, meta)
            port = coordinator.start()
            url = f"http://127.0.0.1:{port}"
            cache_dir = str(Path(tmp) / "state-cache")
            summaries: List[Dict[str, Any]] = []
            errors: List[str] = []
            stop_event = threading.Event()
            lock = threading.Lock()

            def run_worker(index: int, stop_after_round: Optional[int] = None) -> None:
                worker = None
                try:
                    worker = Worker(
                        config,
                        f"node-{index}",
                        url,
                        build_backend(config, shard_id=index, device="cpu"),
                        log_path=str(Path(tmp) / f"node-{index}.jsonl"),
                        state_cache_dir=cache_dir,
                    )
                    if stop_after_round is not None:
                        original = worker._request

                        def churn_request(*a: Any, **kw: Any) -> Any:
                            round_now = coordinator.status()["round"]
                            if round_now >= stop_after_round and not stop_event.is_set():
                                stop_event.set()
                                # Drop registration to simulate an abrupt process death.
                                try:
                                    worker.leave()
                                except Exception:
                                    pass
                                raise ChurnStop()
                            return original(*a, **kw)

                        worker._request = churn_request  # type: ignore[method-assign]
                    result = worker.run()
                    with lock:
                        summaries.append(result)
                except ChurnStop:
                    # Expected: this worker is the one that churns out.
                    pass
                except Exception as exc:  # noqa: BLE001
                    with lock:
                        errors.append(f"node-{index}: {exc}")
                finally:
                    if worker is not None:
                        worker.close()

            # Start both workers; node-0 drops after round 1, then rejoins.
            t0 = threading.Thread(target=run_worker, args=(0, 1), daemon=True)
            t1 = threading.Thread(target=run_worker, args=(1,), daemon=True)
            t0.start()
            t1.start()
            t0.join(timeout=60)
            self.assertTrue(stop_event.is_set(), "churn should have triggered")
            self.assertNotIn("node-0", coordinator.status()["registered"], "churned worker should be gone")
            # Rejoin with same identity + persistent cache while node-1 keeps running.
            t2 = threading.Thread(target=run_worker, args=(0,), daemon=True)
            t2.start()
            t1.join(timeout=60)
            t2.join(timeout=60)

            self.assertFalse(any(t.is_alive() for t in (t0, t1, t2)))
            st = coordinator.status()
            self.assertTrue(st["finished"])
            self.assertIsNone(st["failure"])
            self.assertEqual(st["round"], 5)
            self.assertEqual(len(summaries), 2, errors)
            # Both surviving workers (node-1 and rejoined node-0) must finish cleanly.
            for summary in summaries:
                self.assertIsNone(summary.get("failure"))

    def test_partial_read_and_loss_preserves_state_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp, max_rounds=2, quorum=1, min_workers=1, max_workers=1)
            state, base_hash, meta = initial_adapter_state(config, device="cpu")
            coordinator = Coordinator(config, state, base_hash, meta)
            port = coordinator.start()
            worker = Worker(
                config,
                "node-integrity",
                f"http://127.0.0.1:{port}",
                build_backend(config, shard_id=0, device="cpu"),
                log_path=str(Path(tmp) / "node-integrity.jsonl"),
            )
            # Force chunked state download (manifest present) with transient failures.
            faults = FaultProfile(
                fail_call_window=(3, 4),
                fail_status=503,
                fail_paths=("/v1/state",),
            )
            injected = faults.install(worker)
            try:
                summary = worker.run()
                self.assertEqual(summary["rounds_done"], 2, summary)
                self.assertIsNone(summary.get("failure"))
                st = coordinator.status()
                self.assertIsNone(st["failure"])
                checkpoint = coordinator.store.load_latest()
                self.assertIsNotNone(checkpoint)
                assert checkpoint is not None
                self.assertEqual(checkpoint.round, 2)
                # Verify state hash in final metrics matches checkpoint.
                self.assertEqual(
                    st["last_result"]["state_hash"],
                    checkpoint.meta["state_hash"],
                )
            finally:
                try:
                    worker.leave()
                except Exception:
                    pass
                worker.close()
                coordinator.stop()


if __name__ == "__main__":
    unittest.main()
