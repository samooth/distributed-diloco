"""Small tensor helpers shared by the coordinator and workers.

All synchronized adapter states are float32 on CPU. Training can use bf16 or
NF4, but the pseudo-gradient and the outer optimizer stay in fp32 for stable
aggregation and for a portable wire format.
"""
from __future__ import annotations

import hashlib
import math
from typing import Dict, List, Mapping, Sequence

import torch

State = Dict[str, torch.Tensor]


def clone_state(state: Mapping[str, torch.Tensor]) -> State:
    return {
        key: value.detach().to(device="cpu", dtype=torch.float32).contiguous().clone()
        for key, value in state.items()
    }


def assert_same_structure(left: Mapping[str, torch.Tensor], right: Mapping[str, torch.Tensor]) -> None:
    if set(left) != set(right):
        missing = sorted(set(left) - set(right))
        extra = sorted(set(right) - set(left))
        raise ValueError(f"state keys differ; missing={missing[:5]} extra={extra[:5]}")
    for key in left:
        if tuple(left[key].shape) != tuple(right[key].shape):
            raise ValueError(
                f"shape mismatch for {key}: {tuple(left[key].shape)} != {tuple(right[key].shape)}"
            )


def subtract_state(global_state: Mapping[str, torch.Tensor], local_state: Mapping[str, torch.Tensor]) -> State:
    assert_same_structure(global_state, local_state)
    return {
        key: global_state[key].detach().to("cpu", torch.float32)
        - local_state[key].detach().to("cpu", torch.float32)
        for key in sorted(global_state)
    }


def global_norm(state: Mapping[str, torch.Tensor]) -> float:
    total = 0.0
    for value in state.values():
        total += float(value.detach().to("cpu", torch.float32).pow(2).sum().item())
    return math.sqrt(total)


def scale_state(state: Mapping[str, torch.Tensor], factor: float) -> State:
    return {
        key: value.detach().to("cpu", torch.float32).mul(float(factor)).contiguous()
        for key, value in state.items()
    }


def average_state(states: Sequence[Mapping[str, torch.Tensor]], weights: Sequence[float]) -> State:
    if len(states) != len(weights) or not states:
        raise ValueError("states and weights must be non-empty and have the same length")
    if any(weight <= 0 or not math.isfinite(weight) for weight in weights):
        raise ValueError("weights must be positive and finite")
    total = float(sum(weights))
    if total <= 0:
        raise ValueError("sum of weights must be positive")
    out = {
        key: torch.zeros_like(states[0][key].detach().to("cpu", torch.float32))
        for key in sorted(states[0])
    }
    for state, weight in zip(states, weights):
        assert_same_structure(out, state)
        for key in out:
            out[key].add_(
                state[key].detach().to("cpu", torch.float32),
                alpha=float(weight) / total,
            )
    return out


def cosine_to_mean(states: Sequence[Mapping[str, torch.Tensor]], mean: Mapping[str, torch.Tensor]) -> List[float]:
    mean_norm_sq = sum(float(value.pow(2).sum().item()) for value in mean.values())
    if mean_norm_sq <= 0:
        return [0.0 for _ in states]
    result: List[float] = []
    for state in states:
        assert_same_structure(mean, state)
        dot = 0.0
        norm_sq = 0.0
        for key in mean:
            a = state[key].detach().to("cpu", torch.float32)
            b = mean[key].detach().to("cpu", torch.float32)
            dot += float((a * b).sum().item())
            norm_sq += float(a.pow(2).sum().item())
        denom = math.sqrt(max(norm_sq, 0.0) * mean_norm_sq)
        result.append(dot / denom if denom > 0 else 0.0)
    return result


def state_bytes(state: Mapping[str, torch.Tensor]) -> int:
    return int(sum(value.numel() * value.element_size() for value in state.values()))


def hash_state(state: Mapping[str, torch.Tensor]) -> str:
    digest = hashlib.sha256()
    for key in sorted(state):
        value = state[key].detach().to("cpu").contiguous()
        digest.update(key.encode("utf-8"))
        digest.update(str(tuple(value.shape)).encode("ascii"))
        digest.update(str(value.dtype).encode("ascii"))
        digest.update(value.view(torch.uint8).numpy().tobytes())
    return digest.hexdigest()


def ensure_finite(state: Mapping[str, torch.Tensor]) -> None:
    for key, value in state.items():
        if not bool(torch.isfinite(value).all().item()):
            raise FloatingPointError(f"non-finite values in {key}")
