"""Adapter backend interface and factory."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Protocol, Tuple

import torch

from .config import ExperimentConfig
from .tensor_utils import State


class AdapterBackend(Protocol):
    def state(self) -> State: ...

    def load_state(self, state: Mapping[str, torch.Tensor]) -> None: ...

    def train_steps(self, steps: int, round_index: int = 0) -> Dict[str, float]: ...

    def evaluate(self) -> Dict[str, float]: ...

    def reset_optimizer(self) -> None: ...

    def base_fingerprint(self) -> str: ...

    def adapter_meta(self) -> Dict[str, Any]: ...

    def close(self) -> None: ...


def resolve_device(requested: str = "auto") -> str:
    requested = requested.lower()
    if requested == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if requested.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("CUDA requested but torch.cuda.is_available() is false")
    return requested


def build_backend(
    config: ExperimentConfig,
    shard_id: int,
    device: str = "auto",
) -> AdapterBackend:
    kind = config.model.kind
    if kind == "toy":
        from .toy_backend import ToyBackend

        toy_device = "cpu" if device.lower() == "auto" else resolve_device(device)
        return ToyBackend(config, shard_id=shard_id, device=toy_device)
    if kind == "hf":
        from .hf_backend import HFAdapterBackend

        return HFAdapterBackend(config, shard_id=shard_id, device=resolve_device(device))
    raise ValueError(f"unknown backend: {kind!r}")


def initial_adapter_state(
    config: ExperimentConfig,
    device: str = "auto",
) -> Tuple[State, str, Dict[str, Any]]:
    backend = build_backend(config, shard_id=0, device=device)
    try:
        state = backend.state()
        return state, backend.base_fingerprint(), backend.adapter_meta()
    finally:
        backend.close()
