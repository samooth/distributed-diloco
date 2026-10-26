"""Outer Nesterov optimizer for DiLoCo pseudo-gradients."""
from __future__ import annotations

from typing import Dict, Mapping, Optional

import torch

from .tensor_utils import State, assert_same_structure, clone_state, ensure_finite


class OuterOptimizer:
    """SGD with Nesterov momentum over the aggregated pseudo-gradient.

    The pseudo-gradient is ``global - local`` accumulated over H local steps.
    The update is ``theta -= lr * (g + momentum * buffer)`` with
    ``buffer = momentum * buffer + g``, matching ``torch.optim.SGD(nesterov=True)``.
    """

    def __init__(
        self,
        global_state: Mapping[str, torch.Tensor],
        lr: float = 0.7,
        momentum: float = 0.9,
        nesterov: bool = True,
    ) -> None:
        if lr <= 0:
            raise ValueError("outer lr must be > 0")
        if not 0.0 <= momentum < 1.0:
            raise ValueError("outer momentum must be in [0, 1)")
        self.global_state: State = clone_state(global_state)
        self.lr = float(lr)
        self.momentum = float(momentum)
        self.nesterov = bool(nesterov)
        self.buffer: State = {
            key: torch.zeros_like(value) for key, value in self.global_state.items()
        }

    def step(self, pseudo_grad: Mapping[str, torch.Tensor]) -> State:
        assert_same_structure(self.global_state, pseudo_grad)
        with torch.no_grad():
            for key in sorted(self.global_state):
                grad = pseudo_grad[key].detach().to("cpu", torch.float32)
                ensure_finite({key: grad})
                buf = self.buffer[key]
                buf.mul_(self.momentum).add_(grad)
                update = grad + self.momentum * buf if self.nesterov else buf
                self.global_state[key].sub_(self.lr * update)
        ensure_finite(self.global_state)
        return self.global_state

    def state_dict(self) -> Dict[str, torch.Tensor]:
        return {key: value.detach().clone() for key, value in self.buffer.items()}

    def load_state_dict(self, state: Optional[Mapping[str, torch.Tensor]]) -> None:
        if state is None:
            return
        assert_same_structure(self.buffer, state)
        for key in self.buffer:
            self.buffer[key].copy_(state[key].detach().to("cpu", torch.float32))

    def set_global_state(self, state: Mapping[str, torch.Tensor]) -> None:
        assert_same_structure(self.global_state, state)
        for key in self.global_state:
            self.global_state[key].copy_(state[key].detach().to("cpu", torch.float32))
