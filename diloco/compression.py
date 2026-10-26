"""Optional int8 compression for adapter deltas.

Each tensor is scaled independently to the signed int8 range. The scale is
stored as a float32 tensor next to the quantized payload, so decoding does not
need any extra metadata. This is deliberately simple: adapter deltas are small
and per-tensor scales keep the error predictable.
"""
from __future__ import annotations

from typing import Mapping

import torch

from .tensor_utils import State, ensure_finite

SCALE_PREFIX = "__scale__."
INT8_MAX = 127.0


def encode_int8(state: Mapping[str, torch.Tensor]) -> State:
    if not state:
        raise ValueError("cannot compress an empty state")
    ensure_finite(state)
    payload: State = {}
    for key, value in state.items():
        if str(key).startswith(SCALE_PREFIX):
            raise ValueError(f"reserved tensor key: {key}")
        tensor = value.detach().to("cpu", torch.float32).contiguous()
        max_abs = float(tensor.abs().max().item()) if tensor.numel() else 0.0
        scale = max_abs / INT8_MAX if max_abs > 0 else 1.0
        quantized = torch.round(tensor / scale).clamp(-127, 127).to(torch.int8)
        payload[str(key)] = quantized
        payload[f"{SCALE_PREFIX}{key}"] = torch.tensor([scale], dtype=torch.float32)
    return payload


def decode_int8(payload: Mapping[str, torch.Tensor]) -> State:
    if not payload:
        raise ValueError("cannot decompress an empty payload")
    state: State = {}
    scales = {str(key): value for key, value in payload.items() if str(key).startswith(SCALE_PREFIX)}
    quantized = {str(key): value for key, value in payload.items() if not str(key).startswith(SCALE_PREFIX)}
    if not quantized:
        raise ValueError("int8 payload has no quantized tensors")
    for key, value in quantized.items():
        scale_key = f"{SCALE_PREFIX}{key}"
        if scale_key not in scales:
            raise ValueError(f"missing scale for {key}")
        if value.dtype != torch.int8:
            raise ValueError(f"{key} is {value.dtype}, expected int8")
        scale = scales[scale_key].detach().to("cpu", torch.float32).reshape(-1)
        if scale.numel() != 1:
            raise ValueError(f"scale for {key} must contain one value")
        state[key] = value.detach().to("cpu", torch.float32) * float(scale.item())
    extra = sorted(set(scales) - {f"{SCALE_PREFIX}{key}" for key in quantized})
    if extra:
        raise ValueError(f"unused scales: {extra[:5]}")
    ensure_finite(state)
    return state


def int8_error(state: Mapping[str, torch.Tensor], restored: Mapping[str, torch.Tensor]) -> float:
    if set(state) != set(restored):
        raise ValueError("state keys differ")
    total = 0.0
    for key in state:
        diff = state[key].detach().to("cpu", torch.float32) - restored[key].detach().to("cpu", torch.float32)
        total += float(diff.pow(2).sum().item())
    return total**0.5
