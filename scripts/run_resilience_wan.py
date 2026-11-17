#!/usr/bin/env python3
"""End-to-end resilience run through the WAN emulation proxy.

Starts a localhost coordinator, puts ``wan_emulate.py`` in front of it with
configurable latency/jitter/loss/bandwidth, runs N workers against the proxy,
and induces a churn event (one worker drops and rejoins with the same identity
and persistent state cache). Verifies the run completes with no failure and no
state corruption.

This is the data-plane counterpart of ``tests/test_resilience.py`` (in-process
fault injection). For kernel-level impairment use ``tc netem`` via
``scripts/resilience_netem.sh``; the proxy is sufficient for CI/dev.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from diloco.backend import build_backend, initial_adapter_state  # noqa: E402
from diloco.checkpoint import CheckpointStore  # noqa: E402
from diloco.config import ExperimentConfig  # noqa: E402
from diloco.config import CoordinatorConfig, DataConfig, ModelConfig, TrainConfig  # noqa: E402
from diloco.coordinator import ApiError, Coordinator  # noqa: E402
from diloco.worker import Worker  # noqa: E402


class ChurnStop(Exception):
    pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="WAN resilience E2E runner")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--rounds", type=int, default=5)
    p.add_argument("--churn-after-round", type=int, default=1)
    p.add_argument("--delay-ms", type=float, default=40.0)
    p.add_argument("--jitter-ms", type=float, default=15.0)
    p.add_argument("--loss", type=float, default=0.02)
    p.add_argument("--bandwidth-kbps", type=float, default=0.0)
    p.add_argument("--round-timeout-s", type=float, default=60.0)
    p.add_argument("--checkpoint-dir", default="")
    p.add_argument("--log-level", default="INFO")
    return p


def make_config(tmp: Path, args: argparse.Namespace) -> ExperimentConfig:
    return ExperimentConfig(
        run_id="resilience-wan",
        model=ModelConfig(kind="toy", in_features=8, out_features=4, rank=2, alpha=4.0),
        train=TrainConfig(H=3, inner_lr=0.05, batch_size=4, seed=11),
        data=DataConfig(num_shards=args.workers, eval_samples=16, eval_every=1),
        coordinator=CoordinatorConfig(
            host="127.0.0.1",
            port=0,
            min_workers=args.workers,
            max_workers=args.workers + 2,
            quorum=args.workers,
            round_timeout_s=args.round_timeout_s,
            max_rounds=args.rounds,
            checkpoint_dir=str(tmp),
            checkpoint_every=1,
            token="resilience-token",
            idle_timeout_s=60.0,
            registration_timeout_s=60.0,
        ),
    )


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not 0.0 <= args.loss <= 1.0:
        raise SystemExit("--loss must be between 0 and 1")

    tmp = Path(args.checkpoint_dir).expanduser().resolve() if args.checkpoint_dir else Path(tempfile.mkdtemp(prefix="diloco-resilience-"))
    if args.checkpoint_dir and tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)

    cfg = make_config(tmp, args)
    state, base_hash, meta = initial_adapter_state(cfg, device="cpu")
    coordinator = Coordinator(cfg, state, base_hash, meta)
    coord_port = coordinator.start()

    # Pick a free local port for the proxy.
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    proxy_port = sock.getsockname()[1]
    sock.close()
    log_handle = (tmp / "proxy.log").open("w", encoding="utf-8")
    proxy = subprocess.Popen(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "wan_emulate.py"),
            "--listen-host", "127.0.0.1",
            "--listen-port", str(proxy_port),
            "--target-host", "127.0.0.1",
            "--target-port", str(coord_port),
            "--delay-ms", str(args.delay_ms),
            "--jitter-ms", str(args.jitter_ms),
            "--loss", str(args.loss),
            "--bandwidth-kbps", str(args.bandwidth_kbps),
        ],
        cwd=str(PROJECT_ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{proxy_port}"
    logging.info("coordinator=%s proxy=%s", coord_port, url)

    cache_dir = str(tmp / "state-cache")
    summaries: List[Dict[str, Any]] = []
    errors: List[str] = []
    churn_event = threading.Event()
    lock = threading.Lock()

    def run_worker(index: int, churn: bool = False) -> None:
        worker = None
        try:
            worker = Worker(
                cfg,
                f"node-{index}",
                url,
                build_backend(cfg, shard_id=index, device="cpu"),
                log_path=str(tmp / "logs" / f"node-{index}.jsonl"),
                state_cache_dir=cache_dir,
            )
            if churn:
                original = worker._request

                def churn_request(*a: Any, **kw: Any) -> Any:
                    try:
                        round_now = coordinator.status()["round"]
                    except Exception:
                        round_now = 0
                    if round_now >= args.churn_after_round and not churn_event.is_set():
                        churn_event.set()
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
            pass
        except Exception as exc:  # noqa: BLE001
            logging.exception("worker %s failed", index)
            with lock:
                errors.append(f"node-{index}: {exc}")
        finally:
            if worker is not None:
                worker.close()

    threads: List[threading.Thread] = []
    try:
        t0 = threading.Thread(target=run_worker, args=(0, True), daemon=True)
        threads.append(t0)
        t0.start()
        for i in range(1, args.workers):
            t = threading.Thread(target=run_worker, args=(i,), daemon=True)
            threads.append(t)
            t.start()
        t0.join(timeout=args.round_timeout_s * args.rounds + 30)
        if churn_event.is_set():
            rejoin = threading.Thread(target=run_worker, args=(0,), daemon=True)
            threads.append(rejoin)
            rejoin.start()
        for t in threads:
            t.join(timeout=args.round_timeout_s * args.rounds + 30)

        st = coordinator.status()
        ok = (
            not errors
            and not any(t.is_alive() for t in threads)
            and st["finished"]
            and st["failure"] is None
            and st["round"] == args.rounds
        )
        checkpoint = CheckpointStore(tmp).load_latest()
        if checkpoint is not None:
            ok = ok and checkpoint.round == args.rounds
        logging.info(
            "RESILIENCE result=%s rounds=%s errors=%s summaries=%s",
            "OK" if ok else "FAIL", st["round"], errors, len(summaries),
        )
        return 0 if ok else 1
    finally:
        coordinator.stop()
        proxy.terminate()
        try:
            proxy.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proxy.kill()
        log_handle.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
