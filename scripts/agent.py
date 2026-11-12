#!/usr/bin/env python3
"""Small HTTP agent to launch DiLoCo processes from the web UI.

Runs on each machine (coordinator host or worker host). Exposes endpoints to
start/stop coordinator and worker processes and to read their logs. Intended
for trusted LAN/Tailscale environments only.
"""
from __future__ import annotations

import argparse
import base64
import hmac
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
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

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
    AGENT_TOKEN = ""

    def _send(self, status: int, payload: Any, extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, X-DiLoCo-Token, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _matches_token(self, got: str) -> bool:
        token = str(self.AGENT_TOKEN or "")
        if not token or not got:
            return False
        try:
            return hmac.compare_digest(str(got), token)
        except Exception:
            return False

    def _authorized(self) -> bool:
        if not self.AGENT_TOKEN:
            return True
        # X-DiLoCo-Token header or ?token= query param (API/legacy)
        got = self.headers.get("X-DiLoCo-Token", "")
        if not got:
            qs = parse_qs(urlparse(self.path).query)
            got = (qs.get("token") or [""])[-1]
        if got and self._matches_token(str(got)):
            return True
        # HTTP Basic Auth (browser/dashboard login): password = agent token
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                raw = base64.b64decode(auth.split(" ", 1)[1].strip()).decode("utf-8")
                _user, _sep, pwd = raw.partition(":")
                if self._matches_token(pwd):
                    return True
            except Exception:
                pass
        return False

    def _auth_required_headers(self) -> Dict[str, str]:
        if self.AGENT_TOKEN:
            return {"WWW-Authenticate": 'Basic realm="DiLoCo Dashboard", charset="UTF-8"'}
        return {}

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
        route = unquote(urlparse(self.path).path)
        # Allow unauthenticated health checks
        if route == "/health":
            self._send(200, {"ok": True, "processes": len(PROCESSES)})
            return
        # Protect dashboard UI and agent APIs when token is configured
        if self.AGENT_TOKEN and not self._authorized():
            self._send(401, {"error": "unauthorized"}, extra_headers=self._auth_required_headers())
            return
        if route == "/processes":
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
        if not self._authorized():
            self._send(401, {"error": "unauthorized"}, extra_headers=self._auth_required_headers())
            return
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
            coordinator_identity = payload.get("coordinator_identity", "")
            p2p_context = payload.get("p2p_context", "")
            cmd = [PYTHON, "scripts/run_coordinator.py", "--config", config, "--device", device,
                   "--reset", "--max-rounds", str(max_rounds), "--no-wait"]
            if init_state:
                cmd += ["--init-state", init_state]
            if topic:
                cmd += ["--p2p-topic", topic]
            if coordinator_identity:
                cmd += ["--coordinator-identity", coordinator_identity]
            if p2p_context:
                cmd += ["--p2p-context", p2p_context]
            if payload.get("require_node_identity"):
                cmd += ["--require-node-identity"]
            for pk in (payload.get("allow_pubkeys") or []):
                cmd += ["--allow-pubkey", str(pk)]
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
            coordinator_pubkey = payload.get("coordinator_pubkey", "")
            p2p_context = payload.get("p2p_context", "")
            invite = payload.get("invite", "")
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
            if coordinator_pubkey:
                cmd += ["--coordinator-pubkey", coordinator_pubkey]
            if p2p_context:
                cmd += ["--p2p-context", p2p_context]
            if invite:
                cmd += ["--invite", invite]
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
    parser.add_argument("--token", default="", help="dashboard/control auth token (HTTP Basic password, X-DiLoCo-Token header, or ?token=). Protects UI and /processes,/launch/*,/stop; /health remains open")
    parser.add_argument("--open", action="store_true", help="open dashboard in browser")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Handler.AGENT_TOKEN = str(args.token or "")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}/dashboard.html"
    if Handler.AGENT_TOKEN:
        logging.info("agent listening on %s (dashboard protected by token; /health open)", url)
    else:
        logging.info("agent listening on %s (no token set: dashboard/control UNPROTECTED)", url)
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
