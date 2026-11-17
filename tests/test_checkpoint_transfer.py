"""P2.1: chunked/resumable/verified checkpoint artifact transfer tests."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diloco.backend import build_backend, initial_adapter_state  # noqa: E402
from diloco.checkpoint_transfer import (  # noqa: E402
    CheckpointDownloader,
    CheckpointTransferError,
    verify_artifact,
)
from diloco.config import (  # noqa: E402
    CoordinatorConfig,
    DataConfig,
    ExperimentConfig,
    ModelConfig,
    TrainConfig,
)
from diloco.coordinator import ApiError, Coordinator  # noqa: E402
from diloco.worker import Worker  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class CheckpointTransferTests(unittest.TestCase):
    def _run_one_round(self, tmp: str, wire_compression: str = "none") -> tuple[Coordinator, int, ExperimentConfig]:
        config = ExperimentConfig(
            run_id="unit-checkpoint-transfer",
            model=ModelConfig(kind="toy", in_features=8, out_features=4, rank=2, alpha=4.0),
            train=TrainConfig(H=2, inner_lr=0.05, batch_size=4, seed=11),
            data=DataConfig(num_shards=1, eval_samples=8, eval_every=1),
            coordinator=CoordinatorConfig(
                host="127.0.0.1",
                port=0,
                min_workers=1,
                max_workers=1,
                quorum=1,
                round_timeout_s=15.0,
                max_rounds=1,
                checkpoint_dir=tmp,
                checkpoint_every=1,
                token="checkpoint-token",
                idle_timeout_s=30.0,
                wire_compression=wire_compression,
            ),
        )
        state, base_hash, adapter_meta = initial_adapter_state(config, device="cpu")
        coordinator = Coordinator(config, state, base_hash, adapter_meta)
        port = coordinator.start()
        worker = Worker(
            config,
            "node-checkpoint",
            f"http://127.0.0.1:{port}",
            build_backend(config, shard_id=0, device="cpu"),
            log_path=str(Path(tmp) / "node.jsonl"),
        )
        try:
            summary = worker.run()
            self.assertEqual(summary["rounds_done"], 1)
        finally:
            try:
                worker.leave()
            except Exception:
                pass
            worker.close()
        return coordinator, port, config

    def test_checkpoint_meta_and_verified_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator, port, _ = self._run_one_round(tmp)
            try:
                url = f"http://127.0.0.1:{port}"
                downloader = CheckpointDownloader(url, token="checkpoint-token")
                meta = downloader.fetch_meta(0)
                self.assertEqual(int(meta["round"]), 1)
                self.assertEqual(len(meta["files"]), 3)
                for spec in meta["files"]:
                    self.assertGreater(int(spec["size"]), 0)
                    self.assertEqual(len(spec["chunk_hashes"]), 1)
                    self.assertTrue(spec["merkle_root"])

                artifacts = downloader.download_all(0)
                self.assertEqual(set(artifacts), {"adapter.safetensors", "outer.safetensors", "meta.json"})
                for spec in meta["files"]:
                    blob = artifacts[spec["name"]]
                    self.assertEqual(len(blob), int(spec["size"]))
                    self.assertEqual(_sha(blob), spec["sha256"])
                    self.assertTrue(verify_artifact(blob, spec))
            finally:
                coordinator.stop()

    def test_download_resumes_from_partial_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator, port, config = self._run_one_round(tmp)
            try:
                cache_dir = Path(tmp) / "artifact-cache"
                cache_dir.mkdir()
                url = f"http://127.0.0.1:{port}"
                probe = CheckpointDownloader(url, token="checkpoint-token", cache_dir=str(cache_dir))
                meta = probe.fetch_meta(0)
                spec = next(f for f in meta["files"] if f["name"] == "adapter.safetensors")
                cache = cache_dir / f"000001-{str(meta['state_hash'])[:16]}-adapter.safetensors"

                # Simulate an interrupted download: half a chunk already on disk.
                from diloco.coordinator import STATE_BLOB_CHUNK_SIZE

                full_path = Path(config.coordinator.checkpoint_dir) / "checkpoint-000001" / "adapter.safetensors"
                full = full_path.read_bytes()
                if len(full) <= STATE_BLOB_CHUNK_SIZE:
                    # Force a small chunk size only for the test cache prefix.
                    partial = full[: max(1, len(full) // 2)]
                    self.assertGreater(len(partial), 0)
                    cache.write_bytes(partial)
                else:
                    cache.write_bytes(full[:STATE_BLOB_CHUNK_SIZE])

                downloader = CheckpointDownloader(url, token="checkpoint-token", cache_dir=str(cache_dir))
                blob = downloader.download_file(meta, "adapter.safetensors")
                self.assertEqual(_sha(blob), spec["sha256"])
                self.assertEqual(cache.read_bytes(), blob)
            finally:
                coordinator.stop()

    def test_corrupt_cache_is_redownloaded_and_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator, port, _ = self._run_one_round(tmp)
            try:
                cache_dir = Path(tmp) / "artifact-cache"
                cache_dir.mkdir()
                url = f"http://127.0.0.1:{port}"
                downloader = CheckpointDownloader(url, token="checkpoint-token", cache_dir=str(cache_dir))
                meta = downloader.fetch_meta(0)
                spec = next(f for f in meta["files"] if f["name"] == "adapter.safetensors")
                cache = cache_dir / f"000001-{str(meta['state_hash'])[:16]}-adapter.safetensors"
                cache.write_bytes(b"corrupted" * 10)
                blob = downloader.download_file(meta, "adapter.safetensors")
                self.assertEqual(_sha(blob), spec["sha256"])
                self.assertTrue(verify_artifact(blob, spec))
            finally:
                coordinator.stop()

    def test_wire_compressed_checkpoint_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator, port, _ = self._run_one_round(tmp, wire_compression="gzip")
            try:
                url = f"http://127.0.0.1:{port}"
                downloader = CheckpointDownloader(url, token="checkpoint-token")
                artifacts = downloader.download_all(0)
                meta = downloader.fetch_meta(0)
                self.assertEqual(meta["wire_compression"], "gzip")
                for spec in meta["files"]:
                    self.assertTrue(verify_artifact(artifacts[spec["name"]], spec))
                self.assertGreater(downloader.last_wire_bytes, 0)
            finally:
                coordinator.stop()

    def test_file_endpoint_range_etag_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator, port, _ = self._run_one_round(tmp)
            try:
                base = f"http://127.0.0.1:{port}/v1/checkpoint_file"
                q = "?round=1&name=adapter.safetensors&offset=0&length=8"
                req = urllib.request.Request(base + q, headers={"X-DiLoCo-Token": "checkpoint-token"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    self.assertEqual(resp.status, 206)
                    self.assertEqual(len(resp.read()), 8)
                    etag = resp.headers.get("ETag")
                    self.assertIsNotNone(etag)
                    self.assertIn("bytes 0-7/", resp.headers.get("Content-Range", ""))
                # If-None-Match -> 304 (urllib surfaces it as HTTPError)
                req = urllib.request.Request(
                    base + q,
                    headers={"X-DiLoCo-Token": "checkpoint-token", "If-None-Match": etag},
                )
                with self.assertRaises(urllib.error.HTTPError) as cached:
                    urllib.request.urlopen(req, timeout=10)
                self.assertEqual(cached.exception.code, 304)
                # Invalid offset -> 416
                bad = base + "?round=1&name=adapter.safetensors&offset=99999999&length=8"
                req = urllib.request.Request(bad, headers={"X-DiLoCo-Token": "checkpoint-token"})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(req, timeout=10)
                self.assertEqual(caught.exception.code, 416)
                # Invalid file name -> 400
                bad = base + "?round=1&name=../../etc/passwd&offset=0&length=8"
                req = urllib.request.Request(bad, headers={"X-DiLoCo-Token": "checkpoint-token"})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(req, timeout=10)
                self.assertEqual(caught.exception.code, 400)
            finally:
                coordinator.stop()

    def test_checkpoint_meta_unauthorized(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator, port, _ = self._run_one_round(tmp)
            try:
                url = f"http://127.0.0.1:{port}"
                req = urllib.request.Request(url + "/v1/checkpoint_meta?round=0")
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(req, timeout=10)
                self.assertEqual(caught.exception.code, 401)
            finally:
                coordinator.stop()


if __name__ == "__main__":
    unittest.main()
