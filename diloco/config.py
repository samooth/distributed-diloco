"""Configuration objects for the DiLoCo experiment.

The same JSON/YAML file is used by the coordinator and every worker. The
fingerprint covers model, training and data settings (not the coordinator's
host/port), so a worker with a different model or recipe is rejected during
registration.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple, Union

DEFAULT_TARGET_MODULES: Tuple[str, ...] = (
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "gate_proj",
    "up_proj",
    "down_proj",
)


@dataclass(frozen=True)
class ModelConfig:
    kind: str = "toy"
    name_or_path: str = ""
    in_features: int = 16
    out_features: int = 8
    rank: int = 4
    alpha: float = 4.0
    dtype: str = "auto"
    quant: str = "none"
    lora_dropout: float = 0.0
    target_modules: Tuple[str, ...] = DEFAULT_TARGET_MODULES
    max_length: int = 256
    trust_remote_code: bool = False
    attn_implementation: str = "sdpa"
    gradient_checkpointing: bool = True
    use_dora: bool = False


@dataclass(frozen=True)
class TrainConfig:
    H: int = 20
    inner_lr: float = 5e-2
    inner_weight_decay: float = 0.0
    outer_lr: float = 0.7
    outer_momentum: float = 0.9
    batch_size: int = 16
    grad_accum: int = 1
    max_grad_norm: float = 1.0
    max_delta_norm: float = 0.0
    seed: int = 42
    reset_inner_optimizer: bool = True
    delta_compression: str = "none"
    top_k_ratio: float = 0.1
    error_feedback: bool = False


@dataclass(frozen=True)
class DataConfig:
    path: str = ""
    eval_path: str = ""
    num_shards: int = 2
    max_samples: int = 0
    eval_samples: int = 16
    eval_every: int = 1
    use_chat_template: bool = False
    shuffle: bool = True


@dataclass(frozen=True)
class CoordinatorConfig:
    host: str = "127.0.0.1"
    port: int = 0
    min_workers: int = 2
    max_workers: int = 8
    quorum: int = 2
    round_timeout_s: float = 120.0
    max_rounds: int = 2
    checkpoint_dir: str = "runs/smoke"
    checkpoint_every: int = 1
    token: str = "dev-token"
    max_body_bytes: int = 1_073_741_824
    idle_timeout_s: float = 600.0
    allow_late_join: bool = False
    checkpoint_retention: int = 5
    checkpoint_object_store: str = ""
    checkpoint_object_prefix: str = "checkpoints"
    tls_cert: str = ""
    tls_key: str = ""
    tls_client_ca: str = ""
    tls_require_client_cert: bool = False
    tls_verify: bool = True


@dataclass(frozen=True)
class ExperimentConfig:
    run_id: str = "smoke"
    model: ModelConfig = field(default_factory=ModelConfig)
    train: TrainConfig = field(default_factory=TrainConfig)
    data: DataConfig = field(default_factory=DataConfig)
    coordinator: CoordinatorConfig = field(default_factory=CoordinatorConfig)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> str:
        payload = {
            "model": asdict(self.model),
            "train": asdict(self.train),
            "data": asdict(self.data),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _tuple(value: Any, default: Tuple[str, ...]) -> Tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return tuple(str(part) for part in value)


def _model_from_dict(raw: Mapping[str, Any]) -> ModelConfig:
    return ModelConfig(
        kind=str(raw.get("kind", "toy")).lower(),
        name_or_path=str(raw.get("name_or_path", "")),
        in_features=int(raw.get("in_features", 16)),
        out_features=int(raw.get("out_features", 8)),
        rank=int(raw.get("rank", 4)),
        alpha=float(raw.get("alpha", 4.0)),
        dtype=str(raw.get("dtype", "auto")).lower(),
        quant=str(raw.get("quant", "none")).lower(),
        lora_dropout=float(raw.get("lora_dropout", 0.0)),
        target_modules=_tuple(raw.get("target_modules"), DEFAULT_TARGET_MODULES),
        max_length=int(raw.get("max_length", 256)),
        trust_remote_code=bool(raw.get("trust_remote_code", False)),
        attn_implementation=str(raw.get("attn_implementation", "sdpa")),
        gradient_checkpointing=bool(raw.get("gradient_checkpointing", True)),
        use_dora=bool(raw.get("use_dora", False)),
    )


def _train_from_dict(raw: Mapping[str, Any]) -> TrainConfig:
    return TrainConfig(
        H=int(raw.get("H", 20)),
        inner_lr=float(raw.get("inner_lr", 5e-2)),
        inner_weight_decay=float(raw.get("inner_weight_decay", 0.0)),
        outer_lr=float(raw.get("outer_lr", 0.7)),
        outer_momentum=float(raw.get("outer_momentum", 0.9)),
        batch_size=int(raw.get("batch_size", 16)),
        grad_accum=int(raw.get("grad_accum", 1)),
        max_grad_norm=float(raw.get("max_grad_norm", 1.0)),
        max_delta_norm=float(raw.get("max_delta_norm", 0.0)),
        seed=int(raw.get("seed", 42)),
        reset_inner_optimizer=bool(raw.get("reset_inner_optimizer", True)),
        delta_compression=str(raw.get("delta_compression", "none")).lower(),
        top_k_ratio=float(raw.get("top_k_ratio", 0.1)),
        error_feedback=bool(raw.get("error_feedback", False)),
    )


def _data_from_dict(raw: Mapping[str, Any]) -> DataConfig:
    return DataConfig(
        path=str(raw.get("path", "")),
        eval_path=str(raw.get("eval_path", "")),
        num_shards=int(raw.get("num_shards", 2)),
        max_samples=int(raw.get("max_samples", 0)),
        eval_samples=int(raw.get("eval_samples", 16)),
        eval_every=int(raw.get("eval_every", 1)),
        use_chat_template=bool(raw.get("use_chat_template", False)),
        shuffle=bool(raw.get("shuffle", True)),
    )


def _coordinator_from_dict(raw: Mapping[str, Any]) -> CoordinatorConfig:
    return CoordinatorConfig(
        host=str(raw.get("host", "127.0.0.1")),
        port=int(raw.get("port", 0)),
        min_workers=int(raw.get("min_workers", 2)),
        max_workers=int(raw.get("max_workers", 8)),
        quorum=int(raw.get("quorum", 2)),
        round_timeout_s=float(raw.get("round_timeout_s", 120.0)),
        max_rounds=int(raw.get("max_rounds", 2)),
        checkpoint_dir=str(raw.get("checkpoint_dir", "runs/smoke")),
        checkpoint_every=int(raw.get("checkpoint_every", 1)),
        token=str(raw.get("token", "dev-token")),
        max_body_bytes=int(raw.get("max_body_bytes", 1_073_741_824)),
        idle_timeout_s=float(raw.get("idle_timeout_s", 600.0)),
        allow_late_join=bool(raw.get("allow_late_join", False)),
        checkpoint_retention=int(raw.get("checkpoint_retention", 5)),
        checkpoint_object_store=str(raw.get("checkpoint_object_store", "")),
        checkpoint_object_prefix=str(raw.get("checkpoint_object_prefix", "checkpoints")),
        tls_cert=str(raw.get("tls_cert", "")),
        tls_key=str(raw.get("tls_key", "")),
        tls_client_ca=str(raw.get("tls_client_ca", "")),
        tls_require_client_cert=bool(raw.get("tls_require_client_cert", False)),
        tls_verify=bool(raw.get("tls_verify", True)),
    )


def config_from_dict(raw: Mapping[str, Any]) -> ExperimentConfig:
    if not isinstance(raw, Mapping):
        raise ValueError("config must be a mapping")
    return ExperimentConfig(
        run_id=str(raw.get("run_id", "smoke")),
        model=_model_from_dict(raw.get("model", {}) or {}),
        train=_train_from_dict(raw.get("train", {}) or {}),
        data=_data_from_dict(raw.get("data", {}) or {}),
        coordinator=_coordinator_from_dict(raw.get("coordinator", {}) or {}),
    )


def _resolve(path: str, base_dir: Optional[Path]) -> str:
    if not path or base_dir is None:
        return path
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return str(candidate)
    return str((base_dir / candidate).resolve())


def load_config(path: Union[str, Path], base_dir: Optional[Union[str, Path]] = None) -> ExperimentConfig:
    p = Path(path).expanduser().resolve()
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("PyYAML is required for YAML configs; use JSON") from exc
        raw = yaml.safe_load(text)
    else:
        raw = json.loads(text)
    cfg = config_from_dict(raw or {})
    base = Path(base_dir).resolve() if base_dir is not None else None
    if base is not None:
        cfg = replace(
            cfg,
            data=replace(
                cfg.data,
                path=_resolve(cfg.data.path, base),
                eval_path=_resolve(cfg.data.eval_path, base),
            ),
            coordinator=replace(
                cfg.coordinator,
                checkpoint_dir=_resolve(cfg.coordinator.checkpoint_dir, base),
            ),
        )
    validate_config(cfg)
    return cfg


def validate_config(cfg: ExperimentConfig) -> None:
    m, t, d, c = cfg.model, cfg.train, cfg.data, cfg.coordinator
    if m.kind not in ("toy", "hf"):
        raise ValueError(f"model.kind must be 'toy' or 'hf', got {m.kind!r}")
    if m.rank < 1:
        raise ValueError("model.rank must be >= 1")
    if m.max_length < 8:
        raise ValueError("model.max_length must be >= 8")
    if m.quant not in ("none", "nf4"):
        raise ValueError("model.quant must be 'none' or 'nf4'")
    if m.dtype not in ("auto", "bf16", "fp16", "fp32"):
        raise ValueError("model.dtype must be auto/bf16/fp16/fp32")
    if t.H < 1:
        raise ValueError("train.H must be >= 1")
    if t.batch_size < 1 or t.grad_accum < 1:
        raise ValueError("train.batch_size and train.grad_accum must be >= 1")
    if t.outer_lr <= 0:
        raise ValueError("train.outer_lr must be > 0")
    if t.delta_compression not in ("none", "int8", "topk", "int8_topk"):
        raise ValueError("train.delta_compression must be none/int8/topk/int8_topk")
    if not 0.0 < t.top_k_ratio <= 1.0:
        raise ValueError("train.top_k_ratio must be in (0, 1]")
    if c.checkpoint_retention < 0:
        raise ValueError("coordinator.checkpoint_retention must be >= 0")
    if c.tls_cert or c.tls_key:
        if not c.tls_cert or not c.tls_key:
            raise ValueError("tls_cert and tls_key must be provided together")
    if not 0.0 <= t.outer_momentum < 1.0:
        raise ValueError("train.outer_momentum must be in [0, 1)")
    if d.num_shards < 1:
        raise ValueError("data.num_shards must be >= 1")
    if d.eval_every < 0:
        raise ValueError("data.eval_every must be >= 0")
    if c.min_workers < 1:
        raise ValueError("coordinator.min_workers must be >= 1")
    if c.max_workers < c.min_workers:
        raise ValueError("coordinator.max_workers < min_workers")
    if not 1 <= c.quorum <= c.min_workers:
        raise ValueError("coordinator.quorum must be in [1, min_workers]")
    if c.max_rounds < 0:
        raise ValueError("coordinator.max_rounds must be >= 0")
    if c.round_timeout_s <= 0:
        raise ValueError("coordinator.round_timeout_s must be > 0")
    if c.checkpoint_every < 1:
        raise ValueError("coordinator.checkpoint_every must be >= 1")
    if c.max_body_bytes < 1024:
        raise ValueError("coordinator.max_body_bytes is too small")
    if not 0 <= c.port <= 65535:
        raise ValueError("coordinator.port must be in [0, 65535]")
    if m.kind == "hf" and not m.name_or_path:
        raise ValueError("model.name_or_path is required for kind='hf'")
    if m.kind == "hf" and not d.path:
        raise ValueError("data.path is required for kind='hf'")
    if m.kind == "hf" and d.num_shards < c.max_workers:
        raise ValueError("data.num_shards must be >= coordinator.max_workers for kind='hf'")


def require_same_fingerprint(cfg: ExperimentConfig, other: str) -> None:
    expected = cfg.fingerprint()
    if expected != other:
        raise ValueError(
            "config fingerprint mismatch: coordinator has "
            f"{other[:12]}…, worker has {expected[:12]}…"
        )
