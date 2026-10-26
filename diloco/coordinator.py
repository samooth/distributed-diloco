"""HTTP coordinator for synchronous DiLoCo rounds.

P1 transport is JSON control messages plus safetensors payloads over HTTP/1.0.
It is deliberately dependency-free and easy to audit. gRPC/mTLS is the planned
P2 upgrade; the aggregation and checkpoint code is transport-independent.
"""
from __future__ import annotations

import hmac
import json
import logging
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import parse_qs, urlparse

import torch

from .checkpoint import Checkpoint, CheckpointStore
from .compression import decode_int8, decompress_state
from .config import ExperimentConfig
from .outer import OuterOptimizer
from .protocol import ProtocolError, hash_blob, pack_state, unpack_state
from .tensor_utils import (
    State,
    average_state,
    clone_state,
    cosine_to_mean,
    ensure_finite,
    global_norm,
    hash_state,
)

LOG = logging.getLogger("diloco.coordinator")


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Coordinator:
    def __init__(
        self,
        config: ExperimentConfig,
        global_state: Mapping[str, torch.Tensor],
        base_hash: str,
        adapter_meta: Optional[Mapping[str, Any]] = None,
        start_round: int = 0,
        outer_state: Optional[Mapping[str, torch.Tensor]] = None,
    ) -> None:
        self.config = config
        self.base_hash = str(base_hash)
        self.adapter_meta = dict(adapter_meta or {})
        self.cond = threading.Condition()
        self.outer = OuterOptimizer(
            global_state,
            lr=float(config.train.outer_lr),
            momentum=float(config.train.outer_momentum),
        )
        if outer_state is not None:
            self.outer.load_state_dict(outer_state)
        self.global_state: State = self.outer.global_state
        self.round_index = int(start_round)
        self.global_step = self.round_index * int(config.train.H)
        self._state_hash = hash_state(self.global_state)
        self.registered: Dict[str, Dict[str, Any]] = {}
        self.updates: Dict[str, Dict[str, Any]] = {}
        self.round_started_at: Optional[float] = None
        self.last_activity = time.time()
        self.finished = self.round_index >= int(config.coordinator.max_rounds)
        self.registration_open = not self.finished
        self.failure: Optional[str] = None
        self.last_result: Optional[Dict[str, Any]] = None
        self.history: List[Dict[str, Any]] = []
        self.store = CheckpointStore(config.coordinator.checkpoint_dir)
        self.server: Optional[_Server] = None
        self.thread: Optional[threading.Thread] = None
        self.port: Optional[int] = None

    # ------------------------------------------------------------------ HTTP
    def start(self) -> int:
        coordinator = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "DiLoCo/0.1"
            protocol_version = "HTTP/1.0"

            def log_message(self, format: str, *args: Any) -> None:
                LOG.debug("%s - %s", self.address_string(), format % args)

            def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
                body = json.dumps(dict(payload), sort_keys=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorize(self) -> None:
                expected = coordinator.config.coordinator.token
                if not expected:
                    return
                received = self.headers.get("X-DiLoCo-Token", "")
                if not hmac.compare_digest(str(received), str(expected)):
                    raise ApiError(401, "invalid coordinator token")

            def _read_body(self, limit: int) -> bytes:
                raw_length = self.headers.get("Content-Length")
                if raw_length is None:
                    raise ApiError(411, "Content-Length is required")
                try:
                    length = int(raw_length)
                except ValueError as exc:
                    raise ApiError(400, "invalid Content-Length") from exc
                if length < 0 or length > limit:
                    raise ApiError(413, f"body exceeds {limit} bytes")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ApiError(400, "truncated body")
                return body

            def _read_json(self, limit: int = 65536) -> Dict[str, Any]:
                body = self._read_body(limit)
                try:
                    value = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ApiError(400, f"invalid JSON: {exc}") from exc
                if not isinstance(value, dict):
                    raise ApiError(400, "JSON body must be an object")
                return value

            def _query(self) -> Dict[str, str]:
                parsed = urlparse(self.path)
                values = parse_qs(parsed.query)
                return {key: parts[-1] for key, parts in values.items() if parts}

            def _route(self) -> str:
                return urlparse(self.path).path

            def do_GET(self) -> None:
                try:
                    route = self._route()
                    if route == "/v1/health":
                        self._send_json(200, coordinator.health())
                        return
                    self._authorize()
                    if route == "/v1/metrics":
                        self._send_json(200, coordinator.status())
                        return
                    if route == "/v1/heartbeat":
                        node_id = self._query().get("node_id", "")
                        self._send_json(200, coordinator.heartbeat(node_id))
                        return
                    if route == "/v1/state":
                        node_id = self._query().get("node_id", "")
                        blob = coordinator.get_state(node_id)
                        self._send_bytes(200, blob, "application/octet-stream")
                        return
                    raise ApiError(404, f"unknown route: {route}")
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as exc:  # noqa: BLE001
                    LOG.exception("GET failed")
                    self._send_json(500, {"error": f"internal error: {exc}"})

            def do_POST(self) -> None:
                try:
                    route = self._route()
                    self._authorize()
                    if route == "/v1/register":
                        payload = self._read_json()
                        result = coordinator.register(
                            node_id=str(payload.get("node_id", "")),
                            config_hash=str(payload.get("config_hash", "")),
                            base_hash=str(payload.get("base_hash", "")),
                        )
                        self._send_json(200, result)
                        return
                    if route == "/v1/update":
                        query = self._query()
                        node_id = query.get("node_id", "")
                        try:
                            round_id = int(query.get("round", "-1"))
                        except ValueError as exc:
                            raise ApiError(400, "invalid round") from exc
                        body = self._read_body(coordinator.config.coordinator.max_body_bytes)
                        meta, tensors = unpack_state(body)
                        if int(meta.get("round", -2)) != round_id:
                            raise ApiError(400, "round mismatch between query and payload")
                        if str(meta.get("node_id", "")) != node_id:
                            raise ApiError(400, "node_id mismatch between query and payload")
                        compression = str(meta.get("compression", "none"))
                        expected = coordinator.config.train.delta_compression
                        if compression != expected:
                            raise ApiError(
                                400,
                                f"compression mismatch: got {compression!r}, expected {expected!r}",
                            )
                        try:
                            tensors = decompress_state(
                                tensors,
                                compression,
                                float(coordinator.config.train.top_k_ratio),
                            )
                            delta = clone_state(tensors)
                        except ValueError as exc:
                            raise ApiError(400, f"invalid compressed delta: {exc}") from exc
                        ensure_finite(delta)
                        result = coordinator.submit(
                            node_id=node_id,
                            round_id=round_id,
                            delta=delta,
                            weight=float(meta.get("weight", 1.0)),
                            train_loss=float(meta.get("train_loss", 0.0)),
                            update_hash=hash_blob(body),
                        )
                        self._send_bytes(200, result, "application/octet-stream")
                        return
                    if route == "/v1/leave":
                        payload = self._read_json()
                        result = coordinator.leave(node_id=str(payload.get("node_id", "")))
                        self._send_json(200, result)
                        return
                    raise ApiError(404, f"unknown route: {route}")
                except ApiError as exc:
                    self._send_json(exc.status, {"error": exc.message})
                except ProtocolError as exc:
                    self._send_json(400, {"error": str(exc)})
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as exc:  # noqa: BLE001
                    LOG.exception("POST failed")
                    self._send_json(500, {"error": f"internal error: {exc}"})

        self.server = _Server(
            (self.config.coordinator.host, int(self.config.coordinator.port)),
            Handler,
        )
        self.port = int(self.server.server_address[1])
        if self.config.coordinator.tls_cert or self.config.coordinator.tls_key:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(
                self.config.coordinator.tls_cert,
                self.config.coordinator.tls_key,
            )
            if self.config.coordinator.tls_client_ca:
                context.load_verify_locations(self.config.coordinator.tls_client_ca)
                context.verify_mode = (
                    ssl.CERT_REQUIRED
                    if self.config.coordinator.tls_require_client_cert
                    else ssl.CERT_OPTIONAL
                )
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        LOG.info(
            "coordinator listening on http://%s:%s (round %s/%s)",
            self.config.coordinator.host,
            self.port,
            self.round_index,
            self.config.coordinator.max_rounds,
        )
        return self.port

    def stop(self) -> None:
        with self.cond:
            if not self.finished:
                self.failure = self.failure or "coordinator stopped before completion"
            self.finished = True
            self.cond.notify_all()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None

    # --------------------------------------------------------------- protocol
    def _state_meta_locked(self) -> Dict[str, Any]:
        return {
            "round": int(self.round_index),
            "global_step": int(self.global_step),
            "H": int(self.config.train.H),
            "max_rounds": int(self.config.coordinator.max_rounds),
            "done": bool(self.finished),
            "state_hash": self._state_hash,
            "base_hash": self.base_hash,
            "config_hash": self.config.fingerprint(),
            "adapter_meta": self.adapter_meta,
        }

    def _registration_locked(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "node_id": None,
            "round": int(self.round_index),
            "global_step": int(self.global_step),
            "H": int(self.config.train.H),
            "max_rounds": int(self.config.coordinator.max_rounds),
            "done": bool(self.finished),
            "state_hash": self._state_hash,
            "registered": sorted(self.registered),
        }

    def register(self, node_id: str, config_hash: str, base_hash: str) -> Dict[str, Any]:
        if not node_id or len(node_id) > 128:
            raise ApiError(400, "node_id must be 1-128 characters")
        expected = self.config.fingerprint()
        if not hmac.compare_digest(str(config_hash), expected):
            raise ApiError(409, "config fingerprint mismatch")
        if not hmac.compare_digest(str(base_hash), self.base_hash):
            raise ApiError(409, "base model fingerprint mismatch")
        with self.cond:
            self._evict_stale_locked()
            if self.failure:
                raise ApiError(503, f"run failed: {self.failure}")
            if self.finished:
                raise ApiError(410, "run is already finished")
            if node_id in self.registered:
                self.registered[node_id]["last_seen"] = time.time()
                info = self._registration_locked()
                info["node_id"] = node_id
                return info
            if not self.registration_open and not self.config.coordinator.allow_late_join:
                raise ApiError(409, "run already started; late join is disabled")
            if len(self.registered) >= int(self.config.coordinator.max_workers):
                raise ApiError(429, "maximum number of workers reached")
            self.registered[node_id] = {
                "registered_at": time.time(),
                "last_seen": time.time(),
            }
            self.last_activity = time.time()
            info = self._registration_locked()
            info["node_id"] = node_id
            LOG.info("registered %s (%s/%s)", node_id, len(self.registered), self.config.coordinator.max_workers)
            self.cond.notify_all()
            return info

    def heartbeat(self, node_id: str) -> Dict[str, Any]:
        with self.cond:
            self._evict_stale_locked()
            self._require_node_locked(node_id)
            if self.failure:
                raise ApiError(503, f"run failed: {self.failure}")
            if self.finished:
                raise ApiError(410, "run is already finished")
            self.registered[node_id]["last_seen"] = time.time()
            self.last_activity = time.time()
            return {
                "ok": True,
                "round": int(self.round_index),
                "pending_updates": len(self.updates),
            }

    def get_state(self, node_id: str) -> bytes:
        with self.cond:
            self._evict_stale_locked()
            self._require_node_locked(node_id)
            if self.failure:
                raise ApiError(503, f"run failed: {self.failure}")
            self.registered[node_id]["last_seen"] = time.time()
            self.last_activity = time.time()
            return pack_state(self._state_meta_locked(), self.global_state)

    def submit(
        self,
        node_id: str,
        round_id: int,
        delta: Mapping[str, torch.Tensor],
        weight: float,
        train_loss: float,
        update_hash: str,
    ) -> bytes:
        if weight <= 0 or not torch.isfinite(torch.tensor(weight)).item():
            raise ApiError(400, "weight must be positive and finite")
        with self.cond:
            self._evict_stale_locked()
            self._require_node_locked(node_id)
            if self.failure:
                raise ApiError(503, f"run failed: {self.failure}")
            if int(round_id) != self.round_index:
                raise ApiError(
                    409,
                    f"stale round {round_id}; current round is {self.round_index}",
                )
            if node_id in self.updates:
                raise ApiError(409, "duplicate update for this round")
            if set(delta) != set(self.global_state):
                raise ApiError(400, "delta keys do not match the global adapter")
            for key, value in delta.items():
                if tuple(value.shape) != tuple(self.global_state[key].shape):
                    raise ApiError(400, f"shape mismatch for {key}")
            self.updates[node_id] = {
                "delta": clone_state(delta),
                "weight": float(weight),
                "train_loss": float(train_loss),
                "update_hash": str(update_hash),
                "received_at": time.time(),
            }
            self.registered[node_id]["last_seen"] = time.time()
            self.last_activity = time.time()
            if self.round_started_at is None:
                self.round_started_at = time.time()
            LOG.info(
                "update %s round=%s weight=%.3f loss=%.6f (%s/%s)",
                node_id,
                round_id,
                weight,
                train_loss,
                len(self.updates),
                self.config.coordinator.quorum,
            )
            if len(self.updates) >= int(self.config.coordinator.quorum):
                self._finalize_locked()
            elif self.round_index == round_id:
                deadline = time.time() + float(self.config.coordinator.round_timeout_s)
                while (
                    self.round_index == round_id
                    and len(self.updates) < int(self.config.coordinator.quorum)
                    and not self.finished
                ):
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        self.failure = (
                            f"round {self.round_index} timed out with "
                            f"{len(self.updates)}/{self.config.coordinator.quorum} updates"
                        )
                        self.finished = True
                        self.cond.notify_all()
                        break
                    self.cond.wait(timeout=min(remaining, 1.0))
                if self.failure:
                    raise ApiError(504, self.failure)
                if self.round_index == round_id and not self.finished:
                    raise ApiError(503, "round did not complete")
            return pack_state(self._state_meta_locked(), self.global_state)

    def _require_node_locked(self, node_id: str) -> None:
        if not node_id or node_id not in self.registered:
            raise ApiError(403, "node is not registered")

    def _finalize_locked(self) -> None:
        round_id = int(self.round_index)
        self.registration_open = False
        node_ids = sorted(self.updates)
        deltas = [self.updates[node_id]["delta"] for node_id in node_ids]
        weights = [self.updates[node_id]["weight"] for node_id in node_ids]
        mean_delta = average_state(deltas, weights)
        cosines = cosine_to_mean(deltas, mean_delta)
        train_losses = [self.updates[node_id]["train_loss"] for node_id in node_ids]
        weighted_loss = sum(loss * weight for loss, weight in zip(train_losses, weights)) / sum(weights)
        sync_seconds = (
            time.time() - self.round_started_at if self.round_started_at is not None else 0.0
        )
        self.outer.step(mean_delta)
        self.global_state = self.outer.global_state
        self.round_index += 1
        self.global_step = self.round_index * int(self.config.train.H)
        self._state_hash = hash_state(self.global_state)
        metrics: Dict[str, Any] = {
            "round": round_id,
            "new_round": self.round_index,
            "global_step": self.global_step,
            "nodes": node_ids,
            "weights": {node_id: float(self.updates[node_id]["weight"]) for node_id in node_ids},
            "delta_norm": global_norm(mean_delta),
            "cosine_to_mean": {
                node_id: float(score) for node_id, score in zip(node_ids, cosines)
            },
            "train_loss": float(weighted_loss),
            "sync_seconds": float(sync_seconds),
            "state_hash": self._state_hash,
            "done": self.round_index >= int(self.config.coordinator.max_rounds),
        }
        self.last_result = metrics
        self.history.append(metrics)
        try:
            self.store.append_metrics(metrics)
            if (
                self.round_index % int(self.config.coordinator.checkpoint_every) == 0
                or metrics["done"]
            ):
                self.store.save(
                    round_index=self.round_index,
                    global_step=self.global_step,
                    state=self.global_state,
                    outer_state=self.outer.state_dict(),
                    config_hash=self.config.fingerprint(),
                    base_hash=self.base_hash,
                    metrics=metrics,
                    adapter_meta=self.adapter_meta,
                )
        except Exception as exc:  # noqa: BLE001
            self.failure = f"checkpoint failed: {exc}"
            self.finished = True
            LOG.exception("checkpoint failed")
            self.cond.notify_all()
            raise ApiError(500, self.failure) from exc
        self._prune_checkpoints_locked()
        self.finished = bool(metrics["done"])
        self.updates.clear()
        self.round_started_at = None
        self.cond.notify_all()
        LOG.info(
            "round %s done: nodes=%s loss=%.6f delta_norm=%.6f sync=%.2fs",
            round_id,
            ",".join(node_ids),
            weighted_loss,
            metrics["delta_norm"],
            sync_seconds,
        )

    def leave(self, node_id: str) -> Dict[str, Any]:
        with self.cond:
            removed = node_id in self.registered
            if removed:
                del self.registered[node_id]
                self.updates.pop(node_id, None)
                self.last_activity = time.time()
                self.cond.notify_all()
            return {"ok": True, "removed": removed}

    def _evict_stale_locked(self) -> None:
        timeout = float(self.config.coordinator.idle_timeout_s)
        if timeout <= 0 or self.finished:
            return
        now = time.time()
        stale = [
            node_id
            for node_id, info in list(self.registered.items())
            if now - float(info.get("last_seen", now)) > timeout
        ]
        for node_id in stale:
            del self.registered[node_id]
            self.updates.pop(node_id, None)
            LOG.warning("evicted stale worker %s", node_id)
        if stale:
            self.cond.notify_all()

    def _prune_checkpoints_locked(self) -> None:
        keep = int(self.config.coordinator.checkpoint_retention)
        if keep > 0:
            self.store.prune(keep)

    # ----------------------------------------------------------------- status
    def health(self) -> Dict[str, Any]:
        with self.cond:
            return {
                "ok": self.failure is None,
                "finished": bool(self.finished),
                "round": int(self.round_index),
                "max_rounds": int(self.config.coordinator.max_rounds),
                "registered": len(self.registered),
                "failure": self.failure,
            }

    def status(self) -> Dict[str, Any]:
        with self.cond:
            return {
                "run_id": self.config.run_id,
                "round": int(self.round_index),
                "global_step": int(self.global_step),
                "max_rounds": int(self.config.coordinator.max_rounds),
                "finished": bool(self.finished),
                "failure": self.failure,
                "registered": sorted(self.registered),
                "pending_updates": sorted(self.updates),
                "quorum": int(self.config.coordinator.quorum),
                "last_result": self.last_result,
                "history": list(self.history),
                "state_hash": self._state_hash,
                "base_hash": self.base_hash,
                "adapter_meta": self.adapter_meta,
            }

    def wait_until_finished(self, idle_timeout_s: Optional[float] = None) -> Dict[str, Any]:
        timeout = float(
            self.config.coordinator.idle_timeout_s if idle_timeout_s is None else idle_timeout_s
        )
        with self.cond:
            while not self.finished:
                if timeout > 0 and time.time() - self.last_activity > timeout:
                    self.failure = f"idle timeout after {timeout:.0f}s"
                    self.finished = True
                    self.cond.notify_all()
                    break
                self.cond.wait(timeout=0.5)
            return self.status()

    @classmethod
    def from_checkpoint(
        cls,
        config: ExperimentConfig,
        checkpoint: Checkpoint,
        base_hash: str,
        adapter_meta: Optional[Mapping[str, Any]] = None,
    ) -> "Coordinator":
        return cls(
            config=config,
            global_state=checkpoint.state,
            base_hash=base_hash,
            adapter_meta=adapter_meta,
            start_round=checkpoint.round,
            outer_state=checkpoint.outer_state,
        )
