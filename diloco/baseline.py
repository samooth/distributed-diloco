"""Centralized LoRA baseline for the DiLoCo comparison.

The baseline uses the same backend, model, LoRA recipe, data and seed as a
DiLoCo run, but trains continuously on all examples. Its step budget is
expressed in the same units as the distributed run: every local step of every
worker counts once.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .backend import build_backend
from .checkpoint import save_initial_state
from .config import ExperimentConfig
from .tensor_utils import hash_state

LOG = logging.getLogger("diloco.baseline")


@dataclass
class CentralizedResult:
    steps: int
    tokens: float
    initial_eval: Dict[str, float]
    final_eval: Dict[str, float]
    history: List[Dict[str, Any]]
    state_hash: str
    adapter_path: Optional[str]
    base_hash: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "steps": self.steps,
            "tokens": self.tokens,
            "initial_eval": self.initial_eval,
            "final_eval": self.final_eval,
            "history": self.history,
            "state_hash": self.state_hash,
            "adapter_path": self.adapter_path,
            "base_hash": self.base_hash,
        }


def default_steps(config: ExperimentConfig) -> int:
    return max(
        1,
        int(config.coordinator.max_rounds)
        * int(config.train.H)
        * int(config.coordinator.quorum),
    )


def default_eval_every(config: ExperimentConfig) -> int:
    return max(
        1,
        int(config.train.H) * int(config.coordinator.quorum),
    )


def _baseline_config(config: ExperimentConfig) -> ExperimentConfig:
    workers = max(1, int(config.coordinator.quorum))
    max_samples = int(config.data.max_samples)
    if max_samples > 0:
        max_samples *= workers
    return replace(
        config,
        data=replace(
            config.data,
            num_shards=1,
            max_samples=max_samples,
        ),
    )


def train_centralized(
    config: ExperimentConfig,
    steps: Optional[int] = None,
    eval_every: Optional[int] = None,
    device: str = "auto",
    out_dir: Optional[Union[str, Path]] = None,
    save: bool = True,
) -> CentralizedResult:
    """Train a standard LoRA run with the same token/step budget."""
    total_steps = int(default_steps(config) if steps is None else steps)
    if total_steps < 1:
        raise ValueError("steps must be >= 1")
    interval = int(default_eval_every(config) if eval_every is None else eval_every)
    if interval < 1:
        raise ValueError("eval_every must be >= 1")

    baseline_cfg = _baseline_config(config)
    backend = build_backend(baseline_cfg, shard_id=0, device=device)
    adapter_path: Optional[str] = None
    try:
        initial_eval = backend.evaluate()
        tokens = 0.0
        history: List[Dict[str, Any]] = []
        for step in range(1, total_steps + 1):
            stats = backend.train_steps(1, round_index=step - 1)
            tokens += float(stats.get("tokens", stats.get("samples", 0.0)))
            if step % interval == 0 or step == total_steps:
                evaluation = backend.evaluate()
                record = {
                    "step": step,
                    "tokens": tokens,
                    "train_loss": float(stats.get("loss", 0.0)),
                    "eval_loss": evaluation.get("eval_loss"),
                }
                history.append(record)
                LOG.info(
                    "centralized step %s/%s train=%.6f eval=%s",
                    step,
                    total_steps,
                    record["train_loss"],
                    "?" if record["eval_loss"] is None else f"{record['eval_loss']:.6f}",
                )
        final_eval = backend.evaluate()
        state = backend.state()
        base_hash = backend.base_fingerprint()
        adapter_meta = backend.adapter_meta()
        if save and out_dir is not None:
            out = Path(out_dir).expanduser().resolve()
            out.mkdir(parents=True, exist_ok=True)
            save_initial_state(
                out,
                state,
                base_hash,
                adapter_meta,
                config.fingerprint(),
            )
            summary = {
                "kind": "centralized_lora",
                "run_id": config.run_id,
                "steps": total_steps,
                "eval_every": interval,
                "tokens": tokens,
                "initial_eval": initial_eval,
                "final_eval": final_eval,
                "history": history,
                "adapters": str(out),
                "state_hash": hash_state(state),
                "base_hash": base_hash,
                "config_hash": config.fingerprint(),
            }
            (out / "baseline.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )
            adapter_path = str(out)
        result = CentralizedResult(
            steps=total_steps,
            tokens=tokens,
            initial_eval=initial_eval,
            final_eval=final_eval,
            history=history,
            state_hash=hash_state(state),
            adapter_path=adapter_path,
            base_hash=base_hash,
        )
        return result
    finally:
        backend.close()
