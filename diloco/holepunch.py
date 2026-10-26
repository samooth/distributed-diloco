"""Optional helpers for the Holepunch sidecar."""
from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIDECAR = PROJECT_ROOT / "network" / "holepunch-sidecar" / "index.js"


class HolepunchSidecar:
    def __init__(
        self,
        mode: str,
        topic: str,
        name: str = "node",
        endpoint: Optional[str] = None,
        run_id: Optional[str] = None,
        target_host: Optional[str] = None,
        target_port: Optional[int] = None,
        listen_host: str = "127.0.0.1",
        listen_port: Optional[int] = None,
        storage: Optional[str] = None,
        directory: Optional[str] = None,
        node_bin: str = "node",
    ) -> None:
        self.mode = mode
        self.topic = topic
        self.name = name
        self.endpoint = endpoint
        self.run_id = run_id
        self.target_host = target_host
        self.target_port = target_port
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.storage = storage
        self.directory = directory
        self.node_bin = node_bin or "node"
        self.process: Optional[subprocess.Popen[str]] = None

    def command(self) -> list[str]:
        binary = shutil.which(self.node_bin) or self.node_bin
        command = [binary, str(SIDECAR), "--mode", self.mode, "--topic", self.topic, "--name", self.name]
        if self.run_id is not None:
            command.extend(["--run-id", self.run_id])
        if self.endpoint is not None:
            command.extend(["--endpoint", self.endpoint])
        if self.target_host is not None:
            command.extend(["--target-host", self.target_host])
        if self.target_port is not None:
            command.extend(["--target-port", str(self.target_port)])
        if self.listen_host is not None:
            command.extend(["--listen-host", self.listen_host])
        if self.listen_port is not None:
            command.extend(["--listen-port", str(self.listen_port)])
        if self.storage is not None:
            command.extend(["--storage", self.storage])
        if self.directory is not None:
            command.extend(["--dir", self.directory])
        return command

    def start(self) -> None:
        if self.process is not None:
            return
        self.process = subprocess.Popen(
            self.command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        self.process = None

    def read_event(self, timeout: float) -> Optional[Dict[str, Any]]:
        process = self.process
        if process is None or process.stdout is None:
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            ready, _, _ = select.select([process.stdout], [], [], min(0.25, remaining))
            if ready:
                line = process.stdout.readline()
                if not line:
                    return None
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
            if process.poll() is not None:
                return None
        return None

    def wait_for_endpoint(self, timeout: float = 30.0, expected_run_id: Optional[str] = None) -> str:
        self.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.read_event(min(1.0, deadline - time.monotonic()))
            if not event:
                continue
            if event.get("event") == "message":
                try:
                    message = json.loads(event["data"])
                except (KeyError, TypeError, json.JSONDecodeError):
                    continue
                if expected_run_id is not None and message.get("runId") not in (None, expected_run_id):
                    continue
                if expected_run_id is not None and "runId" not in message:
                    continue
                endpoint = message.get("endpoint")
                if endpoint:
                    return str(endpoint)
            if event.get("event") == "peer" and event.get("endpoint"):
                return str(event["endpoint"])
        raise TimeoutError(f"no coordinator endpoint announced for topic {self.topic!r}")

    def wait_for_ready(self, timeout: float = 30.0) -> Dict[str, Any]:
        self.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.read_event(min(1.0, deadline - time.monotonic()))
            if event and event.get("event") == "ready":
                return event
            if event and event.get("event") == "tunnel-error":
                raise RuntimeError(str(event.get("message", "tunnel failed")))
        raise TimeoutError(f"sidecar did not become ready for topic {self.topic!r}")

    def __enter__(self) -> "HolepunchSidecar":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def discover_coordinator(topic: str, timeout: float = 30.0, name: str = "worker", expected_run_id: Optional[str] = None) -> str:
    with HolepunchSidecar(mode="announce", topic=topic, name=name) as sidecar:
        return sidecar.wait_for_endpoint(timeout, expected_run_id)
