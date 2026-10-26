"""Tiny frozen-base + LoRA backend for algorithm tests and local smoke runs.

The base is a frozen linear layer. The task is another linear layer whose
difference from the base has rank <= ``lora_rank``, so a LoRA adapter can fit
it exactly. This is not a language model; it exists to exercise the DiLoCo
loop, the wire format and checkpoints without downloading weights.
"""
from __future__ import annotations

import hashlib
import math
from typing import Dict, Mapping

import torch
from torch import nn

from .config import ExperimentConfig
from .inner_optim import AdamW
from .tensor_utils import State, clone_state, hash_state


class _ToyLoRA(nn.Module):
    def __init__(self, in_features: int, out_features: int, rank: int, alpha: float) -> None:
        super().__init__()
        self.base = nn.Linear(in_features, out_features, bias=False)
        self.lora_A = nn.Parameter(torch.empty(rank, in_features))
        self.lora_B = nn.Parameter(torch.zeros(out_features, rank))
        self.scale = float(alpha) / float(rank)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        base_out = self.base(x)
        adapter = (x @ self.lora_A.t()) @ self.lora_B.t()
        return base_out + self.scale * adapter


class ToyBackend:
    def __init__(
        self,
        config: ExperimentConfig,
        shard_id: int = 0,
        device: str = "cpu",
    ) -> None:
        self.config = config
        self.shard_id = int(shard_id)
        self.device = torch.device(device)
        self.model_cfg = config.model
        self.train_cfg = config.train
        self._step = 0
        self._optimizer = None

        generator = torch.Generator(device="cpu")
        generator.manual_seed(self.train_cfg.seed + 17)

        in_features = int(self.model_cfg.in_features)
        out_features = int(self.model_cfg.out_features)
        rank = int(self.model_cfg.rank)
        base_weight = torch.randn(out_features, in_features, generator=generator) * 0.25
        left = torch.randn(out_features, rank, generator=generator) * 0.35
        right = torch.randn(rank, in_features, generator=generator) * 0.35
        target_delta = left @ right
        self.base_weight = base_weight.to(self.device, torch.float32)
        self.teacher = (base_weight + target_delta).to(self.device, torch.float32)

        self.model = _ToyLoRA(
            in_features,
            out_features,
            rank,
            float(self.model_cfg.alpha),
        )
        with torch.no_grad():
            self.model.base.weight.copy_(self.base_weight)
            self.model.lora_A.copy_(
                torch.randn(rank, in_features, generator=generator) * 0.01
            )
            self.model.lora_B.zero_()
        self.model.base.weight.requires_grad_(False)
        self.model.to(self.device)
        self.model.train()

        eval_generator = torch.Generator(device="cpu")
        eval_generator.manual_seed(self.train_cfg.seed + 99991)
        self.eval_x = torch.randn(
            int(self.config.data.eval_samples),
            in_features,
            generator=eval_generator,
        ).to(self.device, torch.float32)
        self.eval_y = self.eval_x @ self.teacher.t()
        self.reset_optimizer()

    def reset_optimizer(self) -> None:
        params = [self.model.lora_A, self.model.lora_B]
        self._optimizer = AdamW(
            params,
            lr=float(self.train_cfg.inner_lr),
            weight_decay=float(self.train_cfg.inner_weight_decay),
        )

    def _batch(self, round_index: int) -> tuple[torch.Tensor, torch.Tensor]:
        generator = torch.Generator(device="cpu")
        seed = (
            int(self.train_cfg.seed)
            + self.shard_id * 100003
            + int(round_index) * 1009
            + self._step
        )
        generator.manual_seed(seed)
        x = torch.randn(
            int(self.train_cfg.batch_size),
            int(self.model_cfg.in_features),
            generator=generator,
        )
        x = x + 0.03 * float(self.shard_id)
        y = x @ self.teacher.cpu().t()
        return x.to(self.device, torch.float32), y.to(self.device, torch.float32)

    def train_steps(self, steps: int, round_index: int = 0) -> Dict[str, float]:
        if steps < 1:
            raise ValueError("steps must be >= 1")
        self.model.train()
        losses = []
        for _ in range(steps):
            x, y = self._batch(round_index)
            assert self._optimizer is not None
            self._optimizer.zero_grad(set_to_none=True)
            prediction = self.model(x)
            loss = torch.nn.functional.mse_loss(prediction, y)
            loss.backward()
            if self.train_cfg.max_grad_norm > 0:
                torch.nn.utils.clip_grad_norm_(
                    [self.model.lora_A, self.model.lora_B],
                    float(self.train_cfg.max_grad_norm),
                )
            self._optimizer.step()
            self._step += 1
            losses.append(float(loss.detach().cpu().item()))
        samples = int(steps * self.train_cfg.batch_size)
        mean_loss = sum(losses) / len(losses)
        if not math.isfinite(mean_loss):
            raise FloatingPointError("toy training produced a non-finite loss")
        return {
            "loss": mean_loss,
            "samples": float(samples),
            "tokens": float(samples),
            "weight": float(samples),
        }

    def evaluate(self) -> Dict[str, float]:
        was_training = self.model.training
        self.model.eval()
        with torch.no_grad():
            prediction = self.model(self.eval_x)
            loss = torch.nn.functional.mse_loss(prediction, self.eval_y)
        if was_training:
            self.model.train()
        return {"eval_loss": float(loss.detach().cpu().item())}

    def state(self) -> State:
        return clone_state(
            {
                "lora_A": self.model.lora_A.detach(),
                "lora_B": self.model.lora_B.detach(),
            }
        )

    def load_state(self, state: Mapping[str, torch.Tensor]) -> None:
        if set(state) != {"lora_A", "lora_B"}:
            raise ValueError(f"toy state must contain lora_A and lora_B, got {sorted(state)}")
        with torch.no_grad():
            self.model.lora_A.copy_(
                state["lora_A"].detach().to(self.device, torch.float32)
            )
            self.model.lora_B.copy_(
                state["lora_B"].detach().to(self.device, torch.float32)
            )

    def base_fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(
            f"toy:{self.model_cfg.in_features}:{self.model_cfg.out_features}:"
            f"{self.model_cfg.rank}".encode("ascii")
        )
        digest.update(hash_state({"base": self.base_weight, "teacher": self.teacher}).encode("ascii"))
        return digest.hexdigest()

    def adapter_meta(self) -> Dict[str, object]:
        state = self.state()
        return {
            "kind": "toy",
            "rank": int(self.model_cfg.rank),
            "alpha": float(self.model_cfg.alpha),
            "in_features": int(self.model_cfg.in_features),
            "out_features": int(self.model_cfg.out_features),
            "state_keys": sorted(state),
            "state_bytes": int(sum(v.numel() * v.element_size() for v in state.values())),
            "base_hash": self.base_fingerprint(),
        }

    def close(self) -> None:
        self._optimizer = None
