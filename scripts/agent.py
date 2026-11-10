#!/usr/bin/env python3
"""Small HTTP agent to launch DiLoCo processes from the web UI.

Runs on each machine (coordinator host or worker host). Exposes endpoints to
start/stop coordinator and worker processes and to read their logs. Intended
for trusted LAN/Tailscale environments only.
"""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PYTHON = sys.executable
PROCESSES: Dict[str, Dict[str, Any]] = {}
LOCK = threading.Lock()
WEB_ROOT = PROJECT_ROOT / "web"


def _launch(cmd: List[str], log_path: Path) -> str:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT, text=True, cwd=str(PROJECT_ROOT))
    pid = f"proc-{proc.pid}"
    with LOCK:
        PROCESSES[pid] = {"pid": proc.pid, "cmd": cmd, "proc": proc, "log": str(log_path), "started": time.time()}
    return pid


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-DiLoCo-Token, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self._send(204, {})

    def _serve_static(self, route: str) -> bool:
        if route == "/":
            route = "/dashboard.html"
        route = unquote(urlparse(route).path)
        target = (WEB_ROOT / route.lstrip("/")).resolve()
        if WEB_ROOT.resolve() not in target.parents and target != WEB_ROOT.resolve():
            return False
        if not target.is_file():
            return False
        body = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"ok": True, "processes": len(PROCESSES)})
            return
        if self.path == "/processes":
            out = []
            with LOCK:
                for pid, info in PROCESSES.items():
                    proc = info["proc"]
                    out.append({
                        "id": pid,
                        "pid": info["pid"],
                        "cmd": info["cmd"],
                        "running": proc.poll() is None,
                        "returncode": proc.returncode,
                        "log": info["log"],
                        "started": info["started"],
                    })
            self._send(200, out)
            return
        if self._serve_static(self.path):
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return

        if self.path == "/launch/coordinator":
            config = payload.get("config", "configs/smoke.json")
            max_rounds = payload.get("max_rounds", 4)
            device = payload.get("device", "cpu")
            topic = payload.get("p2p_topic")
            init_state = payload.get("init_state", "")
            cmd = [PYTHON, "scripts/run_coordinator.py", "--config", config, "--device", device,
                   "--reset", "--max-rounds", str(max_rounds), "--no-wait"]
            if init_state:
                cmd += ["--init-state", init_state]
            if topic:
                cmd += ["--p2p-topic", topic]
            log_path = PROJECT_ROOT / "runs" / "agent" / f"coordinator-{int(time.time())}.log"
            pid = _launch(cmd, log_path)
            self._send(200, {"id": pid, "cmd": cmd})
            return

        if self.path == "/launch/worker":
            config = payload.get("config", "configs/smoke.json")
            node_id = payload.get("node_id", "node-0")
            shard_id = payload.get("shard_id", 0)
            device = payload.get("device", "cpu")
            coordinator = payload.get("coordinator", "")
            topic = payload.get("p2p_topic")
            tunnel_port = payload.get("tunnel_listen_port", 0)
            init_state = payload.get("init_state", "")
            node_identity = payload.get("node_identity", "")
            cmd = [PYTHON, "scripts/run_worker.py", "--config", config,
                   "--node-id", node_id, "--shard-id", str(shard_id), "--device", device]
            if coordinator:
                cmd += ["--coordinator", coordinator]
            if topic:
                cmd += ["--p2p-topic", topic]
            if tunnel_port:
                cmd += ["--tunnel-listen-port", str(tunnel_port)]
            if init_state:
                cmd += ["--init-state", init_state]
            if node_identity:
                cmd += ["--node-identity", node_identity]
            log_path = PROJECT_ROOT / "runs" / "agent" / f"{node_id}-{int(time.time())}.log"
            pid = _launch(cmd, log_path)
            self._send(200, {"id": pid, "cmd": cmd})
            return

        if self.path == "/stop":
            pid = payload.get("id")
            with LOCK:
                info = PROCESSES.get(pid)
            if not info:
                self._send(404, {"error": "not found"})
                return
            try:
                os.kill(info["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
            self._send(200, {"ok": True})
            return

        self._send(404, {"error": "not found"})


def main() -> int:
    parser = argparse.ArgumentParser(description="DiLoCo process agent for web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="open dashboard in browser")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}/dashboard.html"
    logging.info("agent listening on %s (serves web/ + /health, /processes, /launch/*, /stop)", url)
    if args.open:
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            logging.warning("could not open browser: %s", exc)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
