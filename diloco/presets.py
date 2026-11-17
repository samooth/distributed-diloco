"""Ready-to-run recipe presets so users never hand-edit JSON config files."""
from __future__ import annotations

from typing import Any, Dict

PRESETS: Dict[str, Dict[str, Any]] = {
    "toy": {
        "run_id": "toy-preset",
        "model": {"kind": "toy", "in_features": 16, "out_features": 8, "rank": 4, "alpha": 4.0},
        "train": {"H": 20, "inner_lr": 0.05, "batch_size": 16, "outer_lr": 0.7},
        "data": {"num_shards": 2, "eval_samples": 32, "eval_every": 1},
        "coordinator": {"min_workers": 2, "max_workers": 4, "quorum": 2, "max_rounds": 4, "round_timeout_s": 60.0},
    },
    "7b-lora": {
        "run_id": "lora-7b",
        "model": {
            "kind": "hf", "name_or_path": "Qwen/Qwen2.5-7B", "rank": 16, "alpha": 32.0,
            "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"], "max_length": 512,
        },
        "train": {"H": 20, "inner_lr": 1e-4, "batch_size": 8, "grad_accum": 2, "outer_lr": 0.7},
        "data": {"path": "data/train.jsonl", "eval_path": "", "num_shards": 4, "eval_samples": 32},
        "coordinator": {"min_workers": 2, "max_workers": 4, "quorum": 2, "max_rounds": 8, "round_timeout_s": 180.0},
    },
    "32b-qlora": {
        "run_id": "qlora-32b",
        "model": {
            "kind": "hf", "name_or_path": "Qwen/Qwen2.5-32B", "quant": "nf4", "dtype": "bf16",
            "rank": 16, "alpha": 32.0,
            "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
            "max_length": 512, "gradient_checkpointing": True,
        },
        "train": {"H": 20, "inner_lr": 1e-4, "batch_size": 4, "grad_accum": 4, "outer_lr": 0.7},
        "data": {"path": "data/train.jsonl", "eval_path": "", "num_shards": 4, "eval_samples": 32},
        "coordinator": {"min_workers": 2, "max_workers": 4, "quorum": 2, "max_rounds": 8, "round_timeout_s": 300.0},
    },
    "70b-qlora": {
        "run_id": "qlora-70b",
        "model": {
            "kind": "hf", "name_or_path": "meta-llama/Llama-3.3-70B-Instruct", "quant": "nf4", "dtype": "bf16",
            "rank": 8, "alpha": 16.0,
            "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
            "max_length": 256, "gradient_checkpointing": True,
        },
        "train": {"H": 20, "inner_lr": 5e-5, "batch_size": 2, "grad_accum": 8, "outer_lr": 0.7},
        "data": {"path": "data/train.jsonl", "eval_path": "", "num_shards": 4, "eval_samples": 16},
        "coordinator": {"min_workers": 2, "max_workers": 4, "quorum": 2, "max_rounds": 6, "round_timeout_s": 600.0},
    },
}


def preset_names() -> list[str]:
    return sorted(PRESETS)


def build_preset(name: str, **overrides: Any) -> Dict[str, Any]:
    """Return a deep-ish copy of a preset with optional overrides.

    Overrides may be top-level fields (run_id, ...) or nested dicts merged into
    model/train/data/coordinator.
    """
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name!r}; choose one of: {', '.join(preset_names())}")
    result: Dict[str, Any] = {}
    for section, values in PRESETS[name].items():
        result[section] = dict(values) if isinstance(values, dict) else values
    for key, value in overrides.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key].update(value)
        else:
            result[key] = value
    return result
