"""Optional compression for adapter deltas.

Supported modes:
- ``none``: send the dense fp32 delta unchanged.
- ``int8``: per-tensor symmetric int8 quantization.
- ``topk``: keep only the largest-magnitude entries of each tensor.
- ``int8_topk``: top-k selection followed by int8 quantization.

Top-k payloads store flattened indices and values plus the original shape.
Error feedback is handled by the worker; this module only encodes/decodes the
payload that crosses the network.
"""
from __future__ import annotations

from typing import Mapping

import torch

from .tensor_utils import State, ensure_finite

SCALE_PREFIX = "__scale__."
INDICES_SUFFIX = ".__indices__"
VALUES_SUFFIX = ".__values__"
SHAPE_SUFFIX = ".__shape__"
INT8_MAX = 127.0
SUPPORTED_MODES = ("none", "int8", "topk", "int8_topk")


def _numel(shape: torch.Size | tuple[int, ...]) -> int:
    total = 1
    for dim in shape:
        total *= int(dim)
    return total


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
    scales = {
        str(key): value for key, value in payload.items() if str(key).startswith(SCALE_PREFIX)
    }
    quantized = {
        str(key): value for key, value in payload.items() if not str(key).startswith(SCALE_PREFIX)
    }
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


def _topk_count(numel: int, ratio: float) -> int:
    if numel <= 0:
        return 0
    if ratio >= 1.0:
        return numel
    count = int(round(numel * float(ratio)))
    return max(1, min(numel, count))


def encode_topk(
    state: Mapping[str, torch.Tensor],
    ratio: float,
    int8: bool = False,
) -> State:
    if not state:
        raise ValueError("cannot compress an empty state")
    if not 0.0 < ratio <= 1.0:
        raise ValueError("top_k_ratio must be in (0, 1]")
    ensure_finite(state)
    payload: State = {}
    for key, value in state.items():
        tensor = value.detach().to("cpu", torch.float32).contiguous()
        flat = tensor.reshape(-1)
        count = _topk_count(flat.numel(), ratio)
        if count >= flat.numel() and flat.numel() > 0:
            indices = torch.arange(flat.numel(), dtype=torch.int64)
        elif flat.numel() == 0:
            indices = torch.empty(0, dtype=torch.int64)
        else:
            _, indices = torch.topk(flat.abs(), count, largest=True, sorted=False)
            indices = indices.to(torch.int64)
        values = flat[indices] if indices.numel() else torch.empty(0, dtype=torch.float32)
        if int8:
            max_abs = float(values.abs().max().item()) if values.numel() else 0.0
            scale = max_abs / INT8_MAX if max_abs > 0 else 1.0
            quantized = torch.round(values / scale).clamp(-127, 127).to(torch.int8)
            payload[f"{key}{VALUES_SUFFIX}"] = quantized
            payload[f"{SCALE_PREFIX}{key}"] = torch.tensor([scale], dtype=torch.float32)
        else:
            payload[f"{key}{VALUES_SUFFIX}"] = values.to(torch.float32).contiguous()
        payload[f"{key}{INDICES_SUFFIX}"] = indices.contiguous()
        payload[f"{key}{SHAPE_SUFFIX}"] = torch.tensor(tuple(tensor.shape), dtype=torch.int64)
    return payload


def decode_topk(payload: Mapping[str, torch.Tensor], int8: bool = False) -> State:
    if not payload:
        raise ValueError("cannot decompress an empty payload")
    bases = {
        key[: -len(INDICES_SUFFIX)]
        for key in payload
        if key.endswith(INDICES_SUFFIX)
    }
    if not bases:
        raise ValueError("top-k payload has no indexed tensors")
    state: State = {}
    for base in sorted(bases):
        indices_key = f"{base}{INDICES_SUFFIX}"
        values_key = f"{base}{VALUES_SUFFIX}"
        shape_key = f"{base}{SHAPE_SUFFIX}"
        if values_key not in payload or shape_key not in payload:
            raise ValueError(f"missing top-k components for {base}")
        indices = payload[indices_key].detach().to("cpu").reshape(-1).to(torch.int64)
        shape = tuple(int(x) for x in payload[shape_key].detach().to("cpu").reshape(-1).tolist())
        numel = _numel(shape)
        if indices.numel() and (indices < 0).any().item():
            raise ValueError(f"negative index for {base}")
        if indices.numel() and (indices >= numel).any().item():
            raise ValueError(f"out of range index for {base}")
        if int8:
            scale_key = f"{SCALE_PREFIX}{base}"
            if scale_key not in payload:
                raise ValueError(f"missing scale for {base}")
            scale = payload[scale_key].detach().to("cpu", torch.float32).reshape(-1)
            if scale.numel() != 1:
                raise ValueError(f"scale for {base} must contain one value")
            values = payload[values_key].detach().to("cpu", torch.float32) * float(scale.item())
        else:
            values = payload[values_key].detach().to("cpu", torch.float32)
        if values.numel() != indices.numel():
            raise ValueError(f"index/value count mismatch for {base}")
        flat = torch.zeros(numel, dtype=torch.float32)
        if indices.numel():
            flat[indices] = values
        state[base] = flat.reshape(shape).contiguous()
    extra = set(payload) - {
        f"{base}{suffix}"
        for base in bases
        for suffix in (INDICES_SUFFIX, VALUES_SUFFIX, SHAPE_SUFFIX, SCALE_PREFIX and f"{SCALE_PREFIX}{base}" and "")
    }
    # Keep validation simple: reject clearly unknown keys.
    unknown = sorted(
        key for key in payload
        if not any(
            key == f"{base}{suffix}"
            for base in bases
            for suffix in (INDICES_SUFFIX, VALUES_SUFFIX, SHAPE_SUFFIX)
        )
        and not key.startswith(SCALE_PREFIX)
    )
    if unknown:
        raise ValueError(f"unused top-k keys: {unknown[:5]}")
    ensure_finite(state)
    return state


def compress_state(state: Mapping[str, torch.Tensor], mode: str, top_k_ratio: float) -> State:
    if mode == "none":
        return {key: value.detach().to("cpu", torch.float32).contiguous().clone() for key, value in state.items()}
    if mode == "int8":
        return encode_int8(state)
    if mode == "topk":
        return encode_topk(state, top_k_ratio, int8=False)
    if mode == "int8_topk":
        return encode_topk(state, top_k_ratio, int8=True)
    raise ValueError(f"unsupported delta compression: {mode!r}")


def decompress_state(state: Mapping[str, torch.Tensor], mode: str, top_k_ratio: float = 1.0) -> State:
    if mode == "none":
        return {key: value.detach().to("cpu", torch.float32).contiguous().clone() for key, value in state.items()}
    if mode == "int8":
        return decode_int8(state)
    if mode == "topk":
        return decode_topk(state, int8=False)
    if mode == "int8_topk":
        return decode_topk(state, int8=True)
    raise ValueError(f"unsupported delta compression: {mode!r}")


def int8_error(state: Mapping[str, torch.Tensor], restored: Mapping[str, torch.Tensor]) -> float:
    if set(state) != set(restored):
        raise ValueError("state keys differ")
    total = 0.0
    for key in state:
        diff = state[key].detach().to("cpu", torch.float32) - restored[key].detach().to("cpu", torch.float32)
        total += float(diff.pow(2).sum().item())
    return total ** 0.5
