from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import torch

from diloco.checkpoint import (
    CheckpointStore,
    load_initial_state,
    save_initial_state,
)
from diloco.compression import (
    compress_state,
    decode_int8,
    decode_topk,
    decompress_state,
    encode_int8,
    encode_topk,
    int8_error,
)
from diloco.config import (
    ExperimentConfig,
    ModelConfig,
    TrainConfig,
    config_from_dict,
)
from diloco.outer import OuterOptimizer
from diloco.protocol import ProtocolError, pack_message, unpack_message
from diloco.tensor_utils import (
    average_state,
    clone_state,
    cosine_to_mean,
    hash_state,
)
from diloco.toy_backend import ToyBackend


class ConfigTests(unittest.TestCase):
    def test_fingerprint_ignores_coordinator_settings(self):
        base = ExperimentConfig()
        changed = replace(base, coordinator=replace(base.coordinator, token="other", port=9999))
        self.assertEqual(base.fingerprint(), changed.fingerprint())

    def test_fingerprint_changes_with_recipe(self):
        base = ExperimentConfig()
        changed = replace(base, train=replace(base.train, H=99))
        self.assertNotEqual(base.fingerprint(), changed.fingerprint())

    def test_invalid_quorum_is_rejected(self):
        from diloco.config import validate_config

        with self.assertRaises(ValueError):
            validate_config(
                config_from_dict(
                    {
                        "coordinator": {
                            "min_workers": 2,
                            "max_workers": 2,
                            "quorum": 3,
                        }
                    }
                )
            )


class OuterOptimizerTests(unittest.TestCase):
    def test_matches_torch_nesterov_sgd(self):
        state = {"w": torch.tensor([1.0, -2.0, 0.5])}
        grad = {"w": torch.tensor([0.25, -0.5, 1.0])}
        outer = OuterOptimizer(state, lr=0.7, momentum=0.9)
        updated = clone_state(outer.step(grad))

        param = torch.nn.Parameter(state["w"].clone())
        optimizer = torch.optim.SGD([param], lr=0.7, momentum=0.9, nesterov=True)
        param.grad = grad["w"].clone()
        optimizer.step()
        self.assertTrue(torch.allclose(updated["w"], param.detach(), atol=1e-6))

    def test_outer_state_roundtrip(self):
        state = {"w": torch.tensor([1.0, 2.0])}
        first = OuterOptimizer(state, lr=0.1, momentum=0.5)
        first.step({"w": torch.tensor([0.3, -0.2])})
        saved = {k: v.clone() for k, v in first.state_dict().items()}

        second = OuterOptimizer(first.global_state, lr=0.1, momentum=0.5)
        second.load_state_dict(saved)
        a = first.step({"w": torch.tensor([0.1, 0.1])})
        b = second.step({"w": torch.tensor([0.1, 0.1])})
        self.assertTrue(torch.allclose(a["w"], b["w"]))


class AggregationTests(unittest.TestCase):
    def test_weighted_average_and_cosine(self):
        left = {"w": torch.tensor([1.0, 0.0])}
        right = {"w": torch.tensor([0.0, 1.0])}
        mean = average_state([left, right], [1.0, 3.0])
        self.assertTrue(torch.allclose(mean["w"], torch.tensor([0.25, 0.75])))
        scores = cosine_to_mean([left, right], mean)
        self.assertAlmostEqual(scores[0], 0.25 / 0.625**0.5, places=6)
        self.assertAlmostEqual(scores[1], 0.75 / 0.625**0.5, places=6)

    def test_average_rejects_bad_weight(self):
        state = {"w": torch.tensor([1.0])}
        with self.assertRaises(ValueError):
            average_state([state], [0.0])


class ProtocolTests(unittest.TestCase):
    def test_roundtrip(self):
        meta = {"round": 3, "node_id": "n0", "weight": 12.5}
        tensors = {"a": torch.arange(4, dtype=torch.float32).reshape(2, 2)}
        blob = pack_message(meta, tensors)
        got_meta, got_tensors = unpack_message(blob)
        self.assertEqual(got_meta, meta)
        self.assertTrue(torch.equal(got_tensors["a"], tensors["a"]))

    def test_bad_magic(self):
        with self.assertRaises(ProtocolError):
            unpack_message(b"NOT-DILOCO")


class CheckpointTests(unittest.TestCase):
    def test_save_load_and_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            state = {"a": torch.tensor([1.0, 2.0])}
            outer = {"a": torch.tensor([0.1, 0.2])}
            first = store.save(1, 10, state, outer, "cfg", "base", {"loss": 1.0})
            second = store.save(2, 20, {"a": state["a"] + 1}, outer, "cfg", "base", {"loss": 0.5})
            self.assertEqual(first.round, 1)
            self.assertEqual(store.latest_round(), 2)
            loaded = store.load_latest()
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.round, 2)
            self.assertEqual(loaded.global_step, 20)
            self.assertTrue(torch.equal(loaded.state["a"], second.state["a"]))
            self.assertEqual(loaded.meta["state_hash"], hash_state(loaded.state))

    def test_initial_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "init"
            state = {"a": torch.tensor([1.0, 2.0])}
            save_initial_state(path, state, "base", {"rank": 4}, "cfg")
            loaded, base_hash, adapter_meta, meta = load_initial_state(path)
            self.assertEqual(base_hash, "base")
            self.assertEqual(adapter_meta, {"rank": 4})
            self.assertEqual(meta["config_hash"], "cfg")
            self.assertEqual(meta["state_hash"], hash_state(loaded))
            self.assertTrue(torch.equal(loaded["a"], state["a"]))

    def test_existing_checkpoint_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(tmp)
            state = {"a": torch.tensor([1.0])}
            store.save(1, 10, state, state, "cfg", "base")
            with self.assertRaises(FileExistsError):
                store.save(1, 10, state, state, "cfg", "base")


class CompressionTests(unittest.TestCase):
    def test_int8_roundtrip_error_is_bounded(self):
        state = {
            "a": torch.tensor([-3.0, -0.5, 0.0, 0.5, 3.0]),
            "b": torch.zeros(3, 2),
        }
        payload = encode_int8(state)
        self.assertEqual(payload["a"].dtype, torch.int8)
        self.assertIn("__scale__.a", payload)
        restored = decode_int8(payload)
        self.assertEqual(set(restored), set(state))
        self.assertLess(int8_error(state, restored), 0.03)
        self.assertTrue(torch.allclose(restored["b"], state["b"]))

    def test_int8_rejects_bad_payload(self):
        with self.assertRaises(ValueError):
            decode_int8({"a": torch.tensor([1], dtype=torch.int8)})
        with self.assertRaises(ValueError):
            decode_int8({"a": torch.tensor([1], dtype=torch.float32), "__scale__.a": torch.tensor([1.0])})


class CompressionModeTests(unittest.TestCase):
    def test_topk_roundtrip(self):
        state = {"a": torch.tensor([-3.0, 0.0, 0.5, 0.0, 3.0])}
        payload = encode_topk(state, 0.4)
        restored = decode_topk(payload)
        self.assertEqual(set(restored), set(state))
        self.assertLess(int8_error(state, restored), 3.1)
        self.assertTrue(torch.equal(restored["a"][1], torch.tensor(0.0)))

    def test_int8_topk_roundtrip(self):
        state = {"a": torch.tensor([-3.0, 0.0, 0.5, 0.0, 3.0])}
        payload = encode_topk(state, 0.4, int8=True)
        restored = decode_topk(payload, int8=True)
        self.assertEqual(set(restored), set(state))
        self.assertLess(int8_error(state, restored), 3.1)

    def test_compress_state_modes(self):
        state = {"a": torch.tensor([1.0, 2.0, 3.0])}
        for mode in ("none", "int8", "topk", "int8_topk"):
            restored = decompress_state(compress_state(state, mode, 0.5), mode, 0.5)
            self.assertEqual(set(restored), set(state))
            self.assertEqual(tuple(restored["a"].shape), (3,))

    def test_topk_rejects_bad_payload(self):
        with self.assertRaises(ValueError):
            decode_topk({"a.__indices__": torch.tensor([0]), "a.__values__": torch.tensor([1.0])})
        payload = encode_topk({"a": torch.tensor([1.0, 2.0])}, 1.0)
        payload["a.__indices__"] = torch.tensor([99])
        with self.assertRaises(ValueError):
            decode_topk(payload)


class ConfigValidationTests(unittest.TestCase):
    def test_topk_ratio_and_tls_are_validated(self):
        from diloco.config import validate_config

        with self.assertRaises(ValueError):
            validate_config(config_from_dict({"train": {"top_k_ratio": 0.0}}))
        with self.assertRaises(ValueError):
            validate_config(config_from_dict({"coordinator": {"tls_cert": "cert.pem"}}))
        with self.assertRaises(ValueError):
            validate_config(config_from_dict({"coordinator": {"checkpoint_retention": -1}}))



    def test_adapter_learns_toy_task(self):
        config = ExperimentConfig(
            model=ModelConfig(kind="toy", in_features=16, out_features=8, rank=4, alpha=4.0),
            train=TrainConfig(H=20, inner_lr=0.05, batch_size=16),
        )
        backend = ToyBackend(config, shard_id=0, device="cpu")
        before = backend.evaluate()["eval_loss"]
        stats = backend.train_steps(20, round_index=0)
        after = backend.evaluate()["eval_loss"]
        self.assertLess(after, before)
        self.assertGreater(stats["weight"], 0)

    def test_state_roundtrip(self):
        config = ExperimentConfig(
            model=ModelConfig(kind="toy", in_features=8, out_features=4, rank=2),
            train=TrainConfig(H=2),
        )
        first = ToyBackend(config, shard_id=0, device="cpu")
        first.train_steps(2, round_index=0)
        state = first.state()
        second = ToyBackend(config, shard_id=0, device="cpu")
        second.load_state(state)
        self.assertEqual(hash_state(first.state()), hash_state(second.state()))


if __name__ == "__main__":
    unittest.main()
