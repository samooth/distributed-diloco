"""Binary wire format: JSON metadata + safetensors payload.

No pickle is used. A message is::

    b"DLCO1" | uint32 header length | UTF-8 JSON header | safetensors bytes

The header carries round ids, weights and metrics; the payload carries the
adapter delta or the global adapter state. safetensors validates offsets and
never executes code.
"""
from __future__ import annotations

import hashlib
import json
import struct
from typing import Any, Dict, Mapping, Tuple

import torch
from safetensors.torch import load as load_safetensors
from safetensors.torch import save as save_safetensors

MAGIC = b"DLCO1"
_HEADER = struct.Struct(">I")
MAX_HEADER_BYTES = 1_048_576


class ProtocolError(ValueError):
    pass


def pack_message(meta: Mapping[str, Any], tensors: Mapping[str, torch.Tensor]) -> bytes:
    clean = {
        key: value.detach().to("cpu").contiguous()
        for key, value in tensors.items()
    }
    payload = save_safetensors(clean)
    header = json.dumps(dict(meta), sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(header) > MAX_HEADER_BYTES:
        raise ProtocolError(f"header too large: {len(header)} bytes")
    return MAGIC + _HEADER.pack(len(header)) + header + payload


def unpack_message(blob: bytes) -> Tuple[Dict[str, Any], Dict[str, torch.Tensor]]:
    if len(blob) < len(MAGIC) + _HEADER.size:
        raise ProtocolError("message is truncated")
    if not blob.startswith(MAGIC):
        raise ProtocolError("bad magic; expected a DiLoCo message")
    start = len(MAGIC)
    (header_len,) = _HEADER.unpack(blob[start : start + _HEADER.size])
    if header_len > MAX_HEADER_BYTES:
        raise ProtocolError("header too large")
    payload_start = start + _HEADER.size + header_len
    if payload_start > len(blob):
        raise ProtocolError("truncated header")
    try:
        meta = json.loads(blob[start + _HEADER.size : payload_start].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid metadata: {exc}") from exc
    if not isinstance(meta, dict):
        raise ProtocolError("metadata must be a JSON object")
    try:
        tensors = load_safetensors(blob[payload_start:])
    except Exception as exc:  # safetensors raises several exception types
        raise ProtocolError(f"invalid safetensors payload: {exc}") from exc
    return meta, tensors


def hash_blob(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def pack_state(meta: Mapping[str, Any], state: Mapping[str, torch.Tensor]) -> bytes:
    return pack_message(meta, state)


def unpack_state(blob: bytes) -> Tuple[Dict[str, Any], Dict[str, torch.Tensor]]:
    return unpack_message(blob)
