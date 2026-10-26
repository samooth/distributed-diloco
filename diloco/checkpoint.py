"""Crash-safe checkpoints.

Each checkpoint directory contains the global adapter, the outer momentum and
a JSON manifest. Files are written to a temporary directory first and then
published with ``os.replace``; ``latest.txt`` is also replaced atomically. No
pickle is involved.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

import torch
from safetensors.torch import load_file, save_file

from .tensor_utils import State, clone_state, hash_state

FORMAT_VERSION = 1


@dataclass
class Checkpoint:
    path: Path
    round: int
    global_step: int
    state: State
    outer_state: State
    meta: Dict[str, Any]


class CheckpointStore:
    def __init__(self, root: Union[str, os.PathLike[str]]) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.latest_file = self.root / "latest.txt"
        self.metrics_file = self.root / "metrics.jsonl"

    def _checkpoint_path(self, round_index: int) -> Path:
        return self.root / f"checkpoint-{round_index:06d}"

    def save(
        self,
        round_index: int,
        global_step: int,
        state: Mapping[str, torch.Tensor],
        outer_state: Mapping[str, torch.Tensor],
        config_hash: str,
        base_hash: str,
        metrics: Optional[Mapping[str, Any]] = None,
        overwrite: bool = False,
        adapter_meta: Optional[Mapping[str, Any]] = None,
    ) -> Checkpoint:
        target = self._checkpoint_path(round_index)
        if target.exists() and not overwrite:
            raise FileExistsError(f"checkpoint already exists: {target}")
        tmp = self.root / f".checkpoint-{round_index:06d}.tmp-{os.getpid()}"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        clean_state = clone_state(state)
        clean_outer = clone_state(outer_state)
        try:
            state_file = tmp / "adapter.safetensors"
            outer_file = tmp / "outer.safetensors"
            save_file(clean_state, str(state_file))
            save_file(clean_outer, str(outer_file))
            meta: Dict[str, Any] = {
                "format_version": FORMAT_VERSION,
                "round": int(round_index),
                "global_step": int(global_step),
                "config_hash": str(config_hash),
                "base_hash": str(base_hash),
                "state_hash": hash_state(clean_state),
                "outer_hash": hash_state(clean_outer),
                "created_at_unix": time.time(),
                "metrics": dict(metrics or {}),
                "adapter_meta": dict(adapter_meta or {}),
                "state_bytes": int(sum(v.numel() * v.element_size() for v in clean_state.values())),
            }
            (tmp / "meta.json").write_text(
                json.dumps(meta, indent=2, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )
            if target.exists():
                shutil.rmtree(target)
            os.replace(tmp, target)
            self._write_latest(round_index)
        except Exception:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            raise
        return self.load(target)

    def _write_latest(self, round_index: int) -> None:
        tmp = self.latest_file.with_suffix(".tmp")
        tmp.write_text(f"{round_index:06d}\n", encoding="utf-8")
        os.replace(tmp, self.latest_file)

    def latest_round(self) -> Optional[int]:
        if self.latest_file.is_file():
            try:
                round_index = int(self.latest_file.read_text(encoding="utf-8").strip())
                if self._checkpoint_path(round_index).is_dir():
                    return round_index
            except ValueError:
                pass
        rounds = [int(p.name.split("-")[-1]) for p in self._dirs()]
        return max(rounds) if rounds else None

    def _dirs(self) -> list[Path]:
        return sorted(
            p for p in self.root.glob("checkpoint-*") if p.is_dir() and not p.name.startswith(".")
        )

    def load_latest(self) -> Optional[Checkpoint]:
        round_index = self.latest_round()
        if round_index is None:
            return None
        path = self._checkpoint_path(round_index)
        if not path.is_dir():
            return None
        return self.load(path)

    def load(self, path: Union[str, os.PathLike[str]]) -> Checkpoint:
        p = Path(path)
        meta_path = p / "meta.json"
        if not meta_path.is_file():
            raise FileNotFoundError(f"missing checkpoint metadata: {meta_path}")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if int(meta.get("format_version", 0)) != FORMAT_VERSION:
            raise ValueError(f"unsupported checkpoint format: {meta.get('format_version')}")
        state = load_file(str(p / "adapter.safetensors"))
        outer = load_file(str(p / "outer.safetensors"))
        state = clone_state(state)
        outer = clone_state(outer)
        expected = meta.get("state_hash")
        actual = hash_state(state)
        if expected and expected != actual:
            raise ValueError(f"state hash mismatch in {p}")
        return Checkpoint(
            path=p,
            round=int(meta["round"]),
            global_step=int(meta.get("global_step", 0)),
            state=state,
            outer_state=outer,
            meta=meta,
        )

    def append_metrics(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(dict(record), sort_keys=True, default=str)
        with self.metrics_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def save_initial_state(
    directory: Union[str, os.PathLike[str]],
    state: Mapping[str, torch.Tensor],
    base_hash: str,
    adapter_meta: Mapping[str, Any],
    config_hash: str,
) -> Path:
    """Save a small adapter bundle that a coordinator can load without the base.

    This is how a 32B/70B coordinator avoids loading the frozen model just to
    discover the initial LoRA shapes. A worker exports the bundle once and the
    coordinator only reads the adapter tensors.
    """
    root = Path(directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    clean = clone_state(state)
    save_file(clean, str(root / "adapter.safetensors"))
    meta = {
        "format_version": FORMAT_VERSION,
        "config_hash": str(config_hash),
        "base_hash": str(base_hash),
        "state_hash": hash_state(clean),
        "adapter_meta": dict(adapter_meta),
        "state_bytes": int(sum(v.numel() * v.element_size() for v in clean.values())),
    }
    (root / "meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return root


def load_initial_state(
    directory: Union[str, os.PathLike[str]],
) -> tuple[State, str, Dict[str, Any], Dict[str, Any]]:
    root = Path(directory).expanduser().resolve()
    meta_path = root / "meta.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"missing initial-state metadata: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if int(meta.get("format_version", 0)) != FORMAT_VERSION:
        raise ValueError(f"unsupported initial-state format: {meta.get('format_version')}")
    state = clone_state(load_file(str(root / "adapter.safetensors")))
    actual = hash_state(state)
    if meta.get("state_hash") and meta["state_hash"] != actual:
        raise ValueError(f"initial state hash mismatch in {root}")
    return (
        state,
        str(meta["base_hash"]),
        dict(meta.get("adapter_meta", {})),
        dict(meta),
    )
