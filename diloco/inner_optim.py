"""Inner AdamW implementations.

torch.optim.AdamW in torch 2.14 touches the current accelerator stream even for
CPU parameters. On a machine whose GPU is full, that raises a CUDA OOM instead
of training on CPU. The toy and CPU-HF paths use this small AdamW instead.
"""
from __future__ import annotations

from typing import List

import torch
from torch import nn


class AdamW:
    def __init__(
        self,
        params: List[nn.Parameter],
        lr: float,
        weight_decay: float = 0.0,
        betas: tuple = (0.9, 0.999),
        eps: float = 1e-8,
    ) -> None:
        self.params = [p for p in params if p.requires_grad]
        if not self.params:
            raise ValueError("AdamW received no trainable parameters")
        self.lr = float(lr)
        self.weight_decay = float(weight_decay)
        self.beta1, self.beta2 = float(betas[0]), float(betas[1])
        self.eps = float(eps)
        self.m = [torch.zeros_like(p) for p in self.params]
        self.v = [torch.zeros_like(p) for p in self.params]
        self.t = 0

    def zero_grad(self, set_to_none: bool = True) -> None:
        for param in self.params:
            if set_to_none:
                param.grad = None
            elif param.grad is not None:
                param.grad.zero_()

    def step(self) -> None:
        self.t += 1
        bias1 = 1.0 - self.beta1**self.t
        bias2 = 1.0 - self.beta2**self.t
        with torch.no_grad():
            for param, m, v in zip(self.params, self.m, self.v):
                if param.grad is None:
                    continue
                grad = param.grad
                if self.weight_decay:
                    param.mul_(1.0 - self.lr * self.weight_decay)
                m.mul_(self.beta1).add_(grad, alpha=1.0 - self.beta1)
                v.mul_(self.beta2).addcmul_(grad, grad, value=1.0 - self.beta2)
                denom = (v / bias2).sqrt_().add_(self.eps)
                param.addcdiv_(m / bias1, denom, value=-self.lr)
