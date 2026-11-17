"""Docker GPU image/compose configuration checks (no build required)."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class DockerGpuTests(unittest.TestCase):
    def test_gpu_artifacts_present(self):
        dockerfile = ROOT / "Dockerfile.gpu"
        entrypoint = ROOT / "scripts" / "docker_entrypoint_gpu.sh"
        compose = ROOT / "deploy" / "docker-compose.gpu.yml"
        docs = ROOT / "docs" / "DOCKER_GPU.md"
        for path in (dockerfile, compose, docs):
            self.assertTrue(path.is_file(), f"missing {path}")
        self.assertTrue(entrypoint.is_file())
        self.assertEqual(stat.S_IMODE(entrypoint.stat().st_mode) & 0o111, 0o111, "entrypoint must be executable")

    def test_dockerfile_has_required_stack(self):
        text = (ROOT / "Dockerfile.gpu").read_text(encoding="utf-8")
        self.assertIn("nvidia/cuda", text)
        self.assertIn("torch==", text)
        self.assertIn(".[hf]", text)
        self.assertIn("npm install", text)
        self.assertIn("setup_22.x", text)  # @bsv/sdk requires Node >=22
        self.assertIn("docker_entrypoint_gpu.sh", text)
        # Torch layer must precede the Node layer so Node bumps reuse the cached wheel.
        self.assertLess(text.index("torch=="), text.index("setup_22.x"))

    def test_compose_config_validates(self):
        if not shutil.which("docker"):
            self.skipTest("docker not available")
        proc = subprocess.run(
            ["docker", "compose", "-f", "deploy/docker-compose.gpu.yml", "config"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[:500])
        self.assertIn("diloco-worker-gpu", proc.stdout)
        self.assertIn("gpus", proc.stdout)

    def test_entrypoint_join_logic(self):
        # Stub python3 so we can inspect the arguments without running the worker.
        entrypoint = ROOT / "scripts" / "docker_entrypoint_gpu.sh"
        with __import__("tempfile").TemporaryDirectory() as tmp:
            stub = Path(tmp) / "python3"
            stub.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
            stub.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
                "APP_DIR": str(ROOT),
                "JOIN_CODE": "didiloco://join?t=abc",
                "NODE_ID": "gpu-node",
                "DEVICE": "cuda",
                "NO_TUNNEL": "1",
            }
            proc = subprocess.run(
                ["bash", str(entrypoint)],
                cwd=str(ROOT), capture_output=True, text=True, env=env, timeout=15,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("join", proc.stdout)
            self.assertIn("didiloco://join?t=abc", proc.stdout)
            self.assertIn("--node-id", proc.stdout)
            self.assertIn("gpu-node", proc.stdout)
            self.assertIn("--no-tunnel", proc.stdout)


if __name__ == "__main__":
    unittest.main()
