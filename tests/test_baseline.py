from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import torch

from diloco.backend import build_backend, initial_adapter_state
from diloco.baseline import train_centralized
from diloco.checkpoint import load_initial_state
from diloco.config import (
    CoordinatorConfig,
    DataConfig,
    ExperimentConfig,
    ModelConfig,
    TrainConfig,
)
from diloco.tensor_utils import hash_state


def toy_config(tmp: str, **train_overrides) -> ExperimentConfig:
    return ExperimentConfig(
        run_id="baseline-test",
        model=ModelConfig(kind="toy", in_features=8, out_features=4, rank=2, alpha=2.0),
        train=TrainConfig(
            H=1,
            inner_lr=0.03,
            batch_size=4,
            seed=11,
            outer_lr=1.0,
            outer_momentum=0.0,
            **train_overrides,
        ),
        data=DataConfig(num_shards=1, eval_samples=16, eval_every=1),
        coordinator=CoordinatorConfig(
            min_workers=1,
            max_workers=1,
            quorum=1,
            max_rounds=1,
            checkpoint_dir=tmp,
            token="unit-token",
        ),
    )


class CentralizedBaselineTests(unittest.TestCase):
    def test_toy_baseline_improves_and_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = toy_config(tmp)
            out = Path(tmp) / "centralized"
            result = train_centralized(
                cfg,
                steps=20,
                eval_every=5,
                device="cpu",
                out_dir=out,
            )
            self.assertEqual(result.steps, 20)
            self.assertEqual(len(result.history), 4)
            self.assertLess(result.final_eval["eval_loss"], result.initial_eval["eval_loss"])
            self.assertTrue((out / "adapter.safetensors").is_file())
            self.assertTrue((out / "meta.json").is_file())
            self.assertTrue((out / "baseline.json").is_file())
            state, base_hash, _, meta = load_initial_state(out)
            self.assertEqual(base_hash, result.base_hash)
            self.assertEqual(meta["state_hash"], result.state_hash)
            self.assertEqual(hash_state(state), result.state_hash)

    def test_one_step_matches_local_lora_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = toy_config(tmp)
            out = Path(tmp) / "centralized"
            result = train_centralized(
                cfg,
                steps=1,
                eval_every=1,
                device="cpu",
                out_dir=out,
            )
            baseline_state, _, _, _ = load_initial_state(out)

            initial_state, _, _ = initial_adapter_state(cfg, device="cpu")
            backend = build_backend(cfg, shard_id=0, device="cpu")
            try:
                backend.load_state(initial_state)
                backend.reset_optimizer()
                backend.train_steps(1, round_index=0)
                local_state = backend.state()
            finally:
                backend.close()

            self.assertEqual(result.state_hash, hash_state(baseline_state))
            for key in local_state:
                self.assertTrue(
                    torch.allclose(baseline_state[key], local_state[key], atol=1e-7, rtol=1e-7),
                    msg=key,
                )


if __name__ == "__main__":
    unittest.main()
