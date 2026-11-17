"""HTTP coordinator for synchronous DiLoCo rounds.

P1 transport is JSON control messages plus safetensors payloads over HTTP/1.0.
It is deliberately dependency-free and easy to audit. gRPC/mTLS is the planned
P2 upgrade; the aggregation and checkpoint code is transport-independent.
"""
from __future__ import annotations

import hashlib
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
from .object_store import create_object_store
from .compression import decode_int8, decompress_state
from .config import ExperimentConfig
try:
    from .grpc_transport import GrpcCoordinatorServer
except RuntimeError:  # grpcio is optional
    GrpcCoordinatorServer = None
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
from .wire import available_wire_codecs, compress_wire, decompress_wire, negotiate_wire_codec, normalize_codec, parse_accept_wire

LOG = logging.getLogger("diloco.coordinator")


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)


STATE_BLOB_CHUNK_SIZE = 4 * 1024 * 1024


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _blob_chunk_hashes(blob: bytes, chunk_size: int = STATE_BLOB_CHUNK_SIZE) -> List[str]:
    if chunk_size <= 0:
        chunk_size = STATE_BLOB_CHUNK_SIZE
    if not blob:
        return [_sha256_hex(b"")]
    return [_sha256_hex(blob[i : i + chunk_size]) for i in range(0, len(blob), chunk_size)]


def _merkle_root_hex(chunk_hashes: List[str]) -> str:
    if not chunk_hashes:
        return _sha256_hex(b"")
    level = [bytes.fromhex(h) for h in chunk_hashes]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        nxt = []
        for i in range(0, len(level), 2):
            nxt.append(hashlib.sha256(level[i] + level[i + 1]).digest())
        level = nxt
    return level[0].hex()


def _state_etag(state_hash: str, round_index: int, blob_hash: str = "") -> str:
    # Strong ETag identifying exact state content/version
    suffix = blob_hash[:16] if blob_hash else str(int(round_index))
    return f'"{state_hash}:{int(round_index)}:{suffix}"'


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
        self._state_blob_cache: Optional[Dict[str, Any]] = None
        self.registered: Dict[str, Dict[str, Any]] = {}
        self.known_nodes: set[str] = set()
        self.updates: Dict[str, Dict[str, Any]] = {}
        self.round_started_at: Optional[float] = None
        self.last_activity = time.time()
        self.finished = self.round_index >= int(config.coordinator.max_rounds)
        self.registration_open = not self.finished
        self.failure: Optional[str] = None
        self.last_result: Optional[Dict[str, Any]] = None
        self.history: List[Dict[str, Any]] = []
        self._round_timeout_ema: Optional[float] = None
        self.store = CheckpointStore(
            config.coordinator.checkpoint_dir,
            object_store=create_object_store(
                config.coordinator.checkpoint_object_store,
                config.coordinator.checkpoint_object_prefix,
            ),
        )
        self.server: Optional[_Server] = None
        self.thread: Optional[threading.Thread] = None
        self.registration_thread: Optional[threading.Thread] = None
        self.grpc_server: Optional[GrpcCoordinatorServer] = None
        self.port: Optional[int] = None

    # ------------------------------------------------------------------ HTTP
    def start(self) -> int:
        if self.config.coordinator.transport == "grpc":
            if GrpcCoordinatorServer is None:
                raise RuntimeError("grpcio is required for transport='grpc'")
            self.grpc_server = GrpcCoordinatorServer(
                self,
                self.config.coordinator.host,
                int(self.config.coordinator.port),
            )
            self.port = int(self.grpc_server.port)
            if self.registration_thread is None and not self.finished and int(self.config.coordinator.min_workers) > 0:
                self.registration_thread = threading.Thread(
                    target=self._registration_monitor,
                    name=f"diloco-registration-{self.config.run_id}",
                    daemon=True,
                )
                self.registration_thread.start()
            LOG.info(
                "gRPC coordinator listening on %s:%s (round %s/%s)",
                self.config.coordinator.host,
                self.port,
                self.round_index,
                self.config.coordinator.max_rounds,
            )
            return self.port

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
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "X-DiLoCo-Token, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_bytes(self, status: int, body: bytes, content_type: str, extra_headers: Optional[Dict[str, str]] = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "X-DiLoCo-Token, Content-Type, Range")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Accept-Ranges", "bytes")
                if extra_headers:
                    for k, v in extra_headers.items():
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_OPTIONS(self) -> None:
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "X-DiLoCo-Token, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Content-Length", "0")
                self.end_headers()

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
                    if route == "/v1/public/status":
                        self._send_json(200, coordinator.public_status())
                        return
                    self._authorize()
                    if route in ("/v1/metrics", "/v1/status"):
                        self._send_json(200, coordinator.status())
                        return
                    if route == "/v1/heartbeat":
                        node_id = self._query().get("node_id", "")
                        self._send_json(200, coordinator.heartbeat(node_id))
                        return
                    if route == "/v1/state_meta":
                        self._send_json(200, coordinator.state_meta())
                        return
                    if route == "/v1/state":
                        node_id = self._query().get("node_id", "")
                        # fetch blob + cache info under lock for consistent metadata/etag
                        with coordinator.cond:
                            coordinator._evict_stale_locked()
                            coordinator._require_node_locked(node_id)
                            if coordinator.failure:
                                raise ApiError(503, f"run failed: {coordinator.failure}")
                            coordinator.registered[node_id]["last_seen"] = time.time()
                            coordinator.last_activity = time.time()
                            blob_info = coordinator._ensure_state_blob_locked()
                            blob = blob_info["blob"]
                            etag = str(blob_info["etag"])
                            state_hash = str(blob_info["state_hash"])
                            round_idx = int(blob_info["round"])
                            blob_hash = str(blob_info["blob_hash"])
                        common_headers = {
                            "ETag": etag,
                            "X-DiLoCo-Round": str(round_idx),
                            "X-DiLoCo-State-Hash": state_hash,
                            "X-DiLoCo-Blob-Hash": blob_hash,
                            "X-DiLoCo-Blob-Size": str(len(blob)),
                            "Cache-Control": "no-store",
                        }
                        inm = (self.headers.get("If-None-Match", "") or "").strip()
                        if inm and inm == etag:
                            self.send_response(304)
                            self.send_header("ETag", etag)
                            self.send_header("X-DiLoCo-Round", str(round_idx))
                            self.send_header("X-DiLoCo-State-Hash", state_hash)
                            self.send_header("X-DiLoCo-Blob-Hash", blob_hash)
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("Accept-Ranges", "bytes")
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                        accept_wire = parse_accept_wire(self.headers.get("X-DiLoCo-Accept-Wire-Compression", ""))
                        wire_codec = negotiate_wire_codec(coordinator.config.coordinator.wire_compression, accept_wire)
                        range_header = self.headers.get("Range", "")
                        if range_header:
                            if not range_header.startswith("bytes="):
                                raise ApiError(416, "invalid range unit")
                            spec = range_header[len("bytes="):].strip()
                            if "," in spec:
                                raise ApiError(416, "multiple ranges not supported")
                            try:
                                start_s, end_s = spec.split("-", 1)
                                if start_s == "":
                                    # suffix range: last N bytes
                                    suffix = int(end_s)
                                    if suffix <= 0:
                                        raise ValueError
                                    start = max(0, len(blob) - suffix)
                                    end = len(blob) - 1
                                else:
                                    start = int(start_s)
                                    end = int(end_s) if end_s else len(blob) - 1
                            except Exception as exc:  # noqa: BLE001
                                raise ApiError(416, "invalid byte range") from exc
                            if start < 0 or end < start or start >= len(blob):
                                self.send_response(416)
                                self.send_header("Content-Range", f"bytes */{len(blob)}")
                                self.send_header("ETag", etag)
                                self.send_header("Accept-Ranges", "bytes")
                                self.send_header("Content-Length", "0")
                                self.end_headers()
                                return
                            end = min(end, len(blob) - 1)
                            chunk = blob[start : end + 1]
                            headers = dict(common_headers)
                            headers["Content-Range"] = f"bytes {start}-{end}/{len(blob)}"
                            if wire_codec:
                                try:
                                    wire_chunk = compress_wire(chunk, wire_codec)
                                    headers["X-DiLoCo-Wire-Compression"] = wire_codec
                                    headers["X-DiLoCo-Wire-Uncompressed-Length"] = str(len(chunk))
                                    self._send_bytes(206, wire_chunk, "application/octet-stream", extra_headers=headers)
                                    return
                                except Exception as exc:  # noqa: BLE001
                                    LOG.warning("wire compression failed for state range response: %s", exc)
                            self._send_bytes(206, chunk, "application/octet-stream", extra_headers=headers)
                            return
                        headers = dict(common_headers)
                        if wire_codec:
                            try:
                                wire_blob = compress_wire(blob, wire_codec)
                                headers["X-DiLoCo-Wire-Compression"] = wire_codec
                                headers["X-DiLoCo-Wire-Uncompressed-Length"] = str(len(blob))
                                self._send_bytes(200, wire_blob, "application/octet-stream", extra_headers=headers)
                                return
                            except Exception as exc:  # noqa: BLE001
                                LOG.warning("wire compression failed for state response: %s", exc)
                        self._send_bytes(200, blob, "application/octet-stream", extra_headers=headers)
                        return
                    if route == "/v1/checkpoint_meta":
                        round_id = int(self._query().get("round", "0") or 0)
                        self._send_json(200, coordinator.checkpoint_meta(round_id))
                        return
                    if route == "/v1/checkpoint_file":
                        query = self._query()
                        round_id = int(query.get("round", "0") or 0)
                        name = str(query.get("name", "") or "")
                        info = coordinator.checkpoint_file(
                            round_id,
                            name,
                            offset=int(query.get("offset", "0") or 0),
                            length=int(query.get("length", "0") or 0),
                        )
                        chunk = bytes(info["data"])
                        total = int(info["total_size"])
                        off = int(info["offset"])
                        end = off + len(chunk) - 1
                        common = {
                            "ETag": f'"{info["sha256"]}"',
                            "X-DiLoCo-Checkpoint-Round": str(info["round"]),
                            "X-DiLoCo-Checkpoint-File": str(info["name"]),
                            "X-DiLoCo-Checkpoint-SHA256": str(info["sha256"]),
                            "X-DiLoCo-Checkpoint-Size": str(total),
                            "Accept-Ranges": "bytes",
                            "Cache-Control": "no-store",
                        }
                        inm = (self.headers.get("If-None-Match", "") or "").strip()
                        if inm and inm == common["ETag"]:
                            self.send_response(304)
                            self.send_header("ETag", common["ETag"])
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                        common["Content-Range"] = f"bytes {off}-{end}/{total}"
                        accept_wire = parse_accept_wire(self.headers.get("X-DiLoCo-Accept-Wire-Compression", ""))
                        wire_codec = negotiate_wire_codec(coordinator.config.coordinator.wire_compression, accept_wire)
                        if wire_codec:
                            try:
                                wire_chunk = compress_wire(chunk, wire_codec)
                                common["X-DiLoCo-Wire-Compression"] = wire_codec
                                common["X-DiLoCo-Wire-Uncompressed-Length"] = str(len(chunk))
                                self._send_bytes(206, wire_chunk, "application/octet-stream", extra_headers=common)
                                return
                            except Exception as exc:  # noqa: BLE001
                                LOG.warning("wire compression failed for checkpoint range: %s", exc)
                        self._send_bytes(206, chunk, "application/octet-stream", extra_headers=common)
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
                            node_pubkey=str(payload.get("node_pubkey", "") or ""),
                            node_sig=str(payload.get("node_sig", "") or ""),
                            sig_alg=str(payload.get("sig_alg", "") or ""),
                            run_id=str(payload.get("run_id", "") or ""),
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
                        wire_body = self._read_body(coordinator.config.coordinator.max_body_bytes)
                        wire_bytes = len(wire_body)
                        wire_codec_req = normalize_codec(
                            self.headers.get("X-DiLoCo-Wire-Compression", "")
                            or self.headers.get("Content-Encoding", "")
                        )
                        body = wire_body
                        if wire_codec_req:
                            try:
                                body = decompress_wire(wire_body, wire_codec_req)
                            except Exception as exc:
                                raise ApiError(400, f"invalid wire-compressed body: {exc}") from exc
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
                            node_sig=str(meta.get("node_sig", "") or ""),
                            update_bytes=wire_bytes,
                            update_blob_bytes=len(body),
                        )
                        accept_wire = parse_accept_wire(self.headers.get("X-DiLoCo-Accept-Wire-Compression", ""))
                        resp_wire_codec = negotiate_wire_codec(coordinator.config.coordinator.wire_compression, accept_wire)
                        if resp_wire_codec:
                            try:
                                wire_result = compress_wire(result, resp_wire_codec)
                                self._send_bytes(200, wire_result, "application/octet-stream", extra_headers={
                                    "X-DiLoCo-Wire-Compression": resp_wire_codec,
                                    "X-DiLoCo-Wire-Uncompressed-Length": str(len(result)),
                                })
                                return
                            except Exception as exc:  # noqa: BLE001
                                LOG.warning("wire compression failed for update response: %s", exc)
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
        if not self.finished and int(self.config.coordinator.min_workers) > 0:
            self.registration_thread = threading.Thread(
                target=self._registration_monitor,
                name=f"diloco-registration-{self.config.run_id}",
                daemon=True,
            )
            self.registration_thread.start()
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
        if self.grpc_server is not None:
            self.grpc_server.stop()
            self.grpc_server = None
        if self.registration_thread is not None:
            self.registration_thread.join(timeout=5)
            self.registration_thread = None

    # --------------------------------------------------------------- protocol
    def _invalidate_state_blob_locked(self) -> None:
        self._state_blob_cache = None

    def _ensure_state_blob_locked(self) -> Dict[str, Any]:
        cache = self._state_blob_cache
        if cache and cache.get("state_hash") == self._state_hash and cache.get("round") == int(self.round_index):
            return cache
        meta = {
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
        blob = pack_state(meta, self.global_state)
        blob_hash = hash_blob(blob)
        chunk_hashes = _blob_chunk_hashes(blob, STATE_BLOB_CHUNK_SIZE)
        merkle_root = _merkle_root_hex(chunk_hashes)
        cache = {
            "round": int(self.round_index),
            "state_hash": self._state_hash,
            "blob": blob,
            "blob_hash": blob_hash,
            "blob_size": len(blob),
            "chunk_size": STATE_BLOB_CHUNK_SIZE,
            "chunk_hashes": chunk_hashes,
            "merkle_root": merkle_root,
            "etag": _state_etag(self._state_hash, self.round_index, blob_hash),
            "meta": meta,
        }
        self._state_blob_cache = cache
        return cache

    def _state_meta_locked(self) -> Dict[str, Any]:
        blob_info = self._ensure_state_blob_locked()
        meta = dict(blob_info["meta"])
        meta["state_blob_hash"] = blob_info["blob_hash"]
        meta["state_blob_bytes"] = int(blob_info["blob_size"])
        meta["state_blob_chunk_size"] = int(blob_info["chunk_size"])
        meta["state_blob_chunk_hashes"] = list(blob_info["chunk_hashes"])
        meta["state_blob_merkle_root"] = str(blob_info["merkle_root"])
        meta["state_etag"] = str(blob_info["etag"])
        meta["wire_compression"] = str(self.config.coordinator.wire_compression)
        meta["wire_compression_supported"] = available_wire_codecs()
        return meta

    def state_meta(self) -> Dict[str, Any]:
        with self.cond:
            return self._state_meta_locked()

    def get_state_meta(self, node_id: str) -> Dict[str, Any]:
        with self.cond:
            self._evict_stale_locked()
            self._require_node_locked(node_id)
            if self.failure:
                raise ApiError(503, f"run failed: {self.failure}")
            self.registered[node_id]["last_seen"] = time.time()
            self.last_activity = time.time()
            return self._state_meta_locked()

    def get_state_chunk(self, node_id: str, offset: int, length: int) -> Dict[str, Any]:
        with self.cond:
            self._evict_stale_locked()
            self._require_node_locked(node_id)
            if self.failure:
                raise ApiError(503, f"run failed: {self.failure}")
            self.registered[node_id]["last_seen"] = time.time()
            self.last_activity = time.time()
            blob_info = self._ensure_state_blob_locked()
            blob = blob_info["blob"]
            total = len(blob)
            off = int(offset)
            if off < 0 or (total == 0 and off != 0) or (total > 0 and off >= total):
                raise ApiError(416, "invalid chunk offset")
            req_len = int(length)
            chunk_size = int(blob_info.get("chunk_size", STATE_BLOB_CHUNK_SIZE) or STATE_BLOB_CHUNK_SIZE)
            if req_len <= 0:
                req_len = chunk_size
            req_len = max(1, min(req_len, 16 * 1024 * 1024))
            end = min(total, off + req_len)
            data = blob[off:end] if total > 0 else b""
            eof = end >= total
            return {
                "data": data,
                "offset": int(off),
                "total_size": int(total),
                "blob_hash": str(blob_info["blob_hash"]),
                "state_hash": str(blob_info["state_hash"]),
                "round": int(blob_info["round"]),
                "eof": bool(eof),
            }

    # ------------------------------------------------------ checkpoint artifacts
    def _checkpoint_dir_locked(self, round_id: int) -> Any:
        """Resolve a checkpoint directory by round (or latest when round <= 0)."""
        if int(round_id) > 0:
            path = self.store._checkpoint_path(int(round_id))
            if not path.is_dir():
                raise ApiError(404, f"checkpoint {round_id} not found")
            return path
        latest = self.store.latest_round()
        if latest is None:
            raise ApiError(404, "no checkpoints available")
        return self.store._checkpoint_path(int(latest))

    def _checkpoint_manifest_locked(self, round_id: int) -> Dict[str, Any]:
        path = self._checkpoint_dir_locked(round_id)
        names = ["adapter.safetensors", "outer.safetensors", "meta.json"]
        files = []
        for name in names:
            fp = path / name
            if not fp.is_file():
                raise ApiError(404, f"checkpoint file missing: {name}")
            size = int(fp.stat().st_size)
            file_bytes = fp.read_bytes()
            digest = _sha256_hex(file_bytes)
            chunk_hashes = _blob_chunk_hashes(file_bytes, STATE_BLOB_CHUNK_SIZE)
            files.append({
                "name": name,
                "size": size,
                "sha256": digest,
                "chunk_size": int(STATE_BLOB_CHUNK_SIZE),
                "chunk_hashes": chunk_hashes,
                "merkle_root": _merkle_root_hex(chunk_hashes),
            })
        meta_path = path / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
        return {
            "round": int(meta.get("round", round_id)),
            "global_step": int(meta.get("global_step", 0)),
            "state_hash": str(meta.get("state_hash", "") or self._state_hash),
            "base_hash": str(meta.get("base_hash", "") or self.base_hash),
            "config_hash": str(meta.get("config_hash", "") or self.config.fingerprint()),
            "checkpoint_dir": str(path),
            "files": files,
            "wire_compression": str(self.config.coordinator.wire_compression),
            "wire_compression_supported": available_wire_codecs(),
        }

    def checkpoint_meta(self, round_id: int = 0) -> Dict[str, Any]:
        with self.cond:
            return self._checkpoint_manifest_locked(round_id)

    def checkpoint_file(
        self,
        round_id: int,
        name: str,
        offset: int = 0,
        length: int = 0,
    ) -> Dict[str, Any]:
        allowed = {"adapter.safetensors", "outer.safetensors", "meta.json"}
        if name not in allowed:
            raise ApiError(400, "invalid checkpoint file name")
        with self.cond:
            path = self._checkpoint_dir_locked(round_id)
            fp = path / name
            if not fp.is_file():
                raise ApiError(404, f"checkpoint file missing: {name}")
            data = fp.read_bytes()
            total = len(data)
            off = int(offset)
            if off < 0 or (total == 0 and off != 0) or (total > 0 and off >= total):
                raise ApiError(416, "invalid chunk offset")
            req_len = int(length)
            if req_len <= 0:
                req_len = STATE_BLOB_CHUNK_SIZE
            req_len = max(1, min(req_len, 16 * 1024 * 1024))
            end = min(total, off + req_len)
            return {
                "data": data[off:end],
                "offset": off,
                "total_size": total,
                "sha256": _sha256_hex(data),
                "round": int(round_id),
                "name": name,
                "eof": end >= total,
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
            "wire_compression": str(self.config.coordinator.wire_compression),
            "wire_compression_supported": available_wire_codecs(),
        }

    def _round_wait_seconds_locked(self) -> float:
        if self.round_started_at is None:
            return 0.0
        return max(0.0, time.time() - float(self.round_started_at))

    def _waiting_nodes_locked(self) -> List[str]:
        # Nodes registered for this run but without an update submitted in the current round.
        return sorted(set(self.registered) - set(self.updates))

    def _round_timeout_cap_locked(self) -> float:
        cap = float(self.config.coordinator.round_timeout_max_s or 0.0)
        if cap <= 0:
            cap = float(self.config.coordinator.round_timeout_s)
        return max(float(self.config.coordinator.round_timeout_min_s), cap)

    def _effective_round_timeout_locked(self) -> float:
        base = float(self.config.coordinator.round_timeout_s)
        if not self.config.coordinator.round_timeout_adaptive:
            return base
        ema = self._round_timeout_ema
        if ema is None or ema <= 0:
            eff = base
        else:
            # add headroom for variance/stragglers while adapting to observed round times
            eff = float(ema) * 2.5
        min_s = float(self.config.coordinator.round_timeout_min_s)
        max_s = self._round_timeout_cap_locked()
        return max(min_s, min(max_s, float(eff)))

    def register(
        self,
        node_id: str,
        config_hash: str,
        base_hash: str,
        node_pubkey: str = "",
        node_sig: str = "",
        sig_alg: str = "",
        run_id: str = "",
    ) -> Dict[str, Any]:
        if not node_id or len(node_id) > 128:
            raise ApiError(400, "node_id must be 1-128 characters")
        expected = self.config.fingerprint()
        if not hmac.compare_digest(str(config_hash), expected):
            raise ApiError(409, "config fingerprint mismatch")
        if not hmac.compare_digest(str(base_hash), self.base_hash):
            raise ApiError(409, "base model fingerprint mismatch")
        node_pubkey = str(node_pubkey or "").strip()
        node_sig = str(node_sig or "").strip()
        sig_alg = str(sig_alg or "").strip()
        run_id = str(run_id or self.config.run_id)
        allowed = tuple(self.config.coordinator.allowed_node_pubkeys or ())
        if self.config.coordinator.require_node_identity and not node_pubkey:
            raise ApiError(403, "node identity is required")
        if allowed and node_pubkey not in allowed:
            raise ApiError(403, "node pubkey is not allowed")
        if node_pubkey and node_sig:
            try:
                from .identity import canonical_register, verify_message
                msg = canonical_register(node_id, str(config_hash), str(base_hash), run_id)
                if not verify_message(node_pubkey, msg, node_sig):
                    raise ApiError(403, "invalid node signature")
            except ApiError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise ApiError(403, f"signature verification failed: {exc}") from exc
        elif self.config.coordinator.require_node_identity:
            # Pubkey provided but no signature while identity required
            raise ApiError(403, "node signature is required")

        with self.cond:
            self._evict_stale_locked()
            if self.failure:
                raise ApiError(503, f"run failed: {self.failure}")
            if self.finished:
                raise ApiError(410, "run is already finished")
            if node_id in self.registered:
                existing = self.registered[node_id]
                existing_pub = str(existing.get("node_pubkey", "") or "")
                if existing_pub and node_pubkey and existing_pub != node_pubkey:
                    raise ApiError(403, "node identity mismatch for rejoin")
                if node_pubkey and not existing_pub:
                    existing["node_pubkey"] = node_pubkey
                existing["last_seen"] = time.time()
                self.known_nodes.add(node_id)
                info = self._registration_locked()
                info["node_id"] = node_id
                return info
            rejoining = node_id in self.known_nodes
            if not self.registration_open and not self.config.coordinator.allow_late_join and not rejoining:
                raise ApiError(409, "run already started; late join is disabled")
            if len(self.registered) >= int(self.config.coordinator.max_workers) and not rejoining:
                raise ApiError(429, "maximum number of workers reached")
            if rejoining and len(self.registered) >= int(self.config.coordinator.max_workers):
                raise ApiError(429, "maximum number of workers reached")
            self.registered[node_id] = {
                "registered_at": time.time(),
                "last_seen": time.time(),
                "node_pubkey": node_pubkey,
            }
            self.known_nodes.add(node_id)
            self.last_activity = time.time()
            info = self._registration_locked()
            info["node_id"] = node_id
            LOG.info("registered %s (%s/%s)%s", node_id, len(self.registered), self.config.coordinator.max_workers, " [identity]" if node_pubkey else "")
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
            return self._ensure_state_blob_locked()["blob"]

    def submit(
        self,
        node_id: str,
        round_id: int,
        delta: Mapping[str, torch.Tensor],
        weight: float,
        train_loss: float,
        update_hash: str,
        node_sig: str = "",
        update_bytes: int = 0,
        update_blob_bytes: int = 0,
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
            node_sig = str(node_sig or "").strip()
            reg_pub = str(self.registered[node_id].get("node_pubkey", "") or "")
            if node_sig and reg_pub:
                try:
                    from .identity import canonical_update, verify_message
                    msg = canonical_update(node_id, int(round_id), "", self.config.fingerprint(), self._state_hash)
                    if not verify_message(reg_pub, msg, node_sig):
                        raise ApiError(403, "invalid update signature")
                except ApiError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise ApiError(403, f"update signature verification failed: {exc}") from exc
            elif self.config.coordinator.require_node_identity and reg_pub:
                raise ApiError(403, "update signature is required")
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
                "update_bytes": int(update_bytes or 0),
                "update_blob_bytes": int(update_blob_bytes or 0),
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
                effective_timeout = self._effective_round_timeout_locked()
                deadline = time.time() + float(effective_timeout)
                while (
                    self.round_index == round_id
                    and len(self.updates) < int(self.config.coordinator.quorum)
                    and not self.finished
                ):
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        have = len(self.updates)
                        can_partial = (
                            bool(self.config.coordinator.allow_partial_round)
                            and have >= int(self.config.coordinator.partial_round_min_updates)
                            and have > 0
                            and self.round_index == round_id
                            and not self.finished
                        )
                        if can_partial:
                            waiting = self._waiting_nodes_locked()
                            LOG.warning(
                                "round %s partial finalize on timeout: %s/%s updates (waiting=%s, timeout=%.1fs)",
                                self.round_index,
                                have,
                                self.config.coordinator.quorum,
                                ",".join(waiting[:12]) + (f",+{len(waiting)-12}" if len(waiting) > 12 else ""),
                                effective_timeout,
                            )
                            self._finalize_locked(partial=True)
                            break
                        waiting = self._waiting_nodes_locked()
                        waiting_info = f"; waiting={','.join(waiting[:12])}" if waiting else ""
                        if waiting and len(waiting) > 12:
                            waiting_info += f",+{len(waiting)-12}"
                        self.failure = (
                            f"round {self.round_index} timed out with "
                            f"{len(self.updates)}/{self.config.coordinator.quorum} updates"
                            f"{waiting_info}"
                        )
                        self.finished = True
                        self.cond.notify_all()
                        break
                    self.cond.wait(timeout=min(remaining, 1.0))
                if self.failure:
                    raise ApiError(504, self.failure)
                if self.round_index == round_id and not self.finished:
                    raise ApiError(503, "round did not complete")
            return self._ensure_state_blob_locked()["blob"]

    def _require_node_locked(self, node_id: str) -> None:
        if not node_id or node_id not in self.registered:
            raise ApiError(403, "node is not registered")

    def _finalize_locked(self, partial: bool = False) -> None:
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
        quorum = int(self.config.coordinator.quorum)
        effective_timeout = float(self._effective_round_timeout_locked())
        self.outer.step(mean_delta)
        self.global_state = self.outer.global_state
        self.round_index += 1
        self.global_step = self.round_index * int(self.config.train.H)
        self._state_hash = hash_state(self.global_state)
        self._invalidate_state_blob_locked()
        state_blob_bytes = int(self._ensure_state_blob_locked().get("blob_size", 0) or 0)
        update_bytes_by_node = {node_id: int(self.updates[node_id].get("update_bytes", 0) or 0) for node_id in node_ids}
        update_bytes_total = int(sum(update_bytes_by_node.values()))
        update_blob_bytes_by_node = {node_id: int(self.updates[node_id].get("update_blob_bytes", 0) or 0) for node_id in node_ids}
        update_blob_bytes_total = int(sum(update_blob_bytes_by_node.values()))
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
            "update_bytes_total": update_bytes_total,
            "update_bytes_by_node": update_bytes_by_node,
            "update_blob_bytes_total": update_blob_bytes_total,
            "update_blob_bytes_by_node": update_blob_bytes_by_node,
            "state_blob_bytes": state_blob_bytes,
            "state_hash": self._state_hash,
            "done": self.round_index >= int(self.config.coordinator.max_rounds),
            "partial": bool(partial),
            "quorum": quorum,
            "updates_received": len(node_ids),
            "effective_round_timeout_s": effective_timeout,
        }
        # Adaptive timeout: update EMA from observed round sync time
        if self.config.coordinator.round_timeout_adaptive and sync_seconds > 0:
            alpha = 0.3
            if self._round_timeout_ema is None:
                self._round_timeout_ema = float(sync_seconds)
            else:
                self._round_timeout_ema = alpha * float(sync_seconds) + (1.0 - alpha) * float(self._round_timeout_ema)
            metrics["adaptive_round_timeout_ema_s"] = float(self._round_timeout_ema)
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
            "round %s done%s: nodes=%s loss=%.6f delta_norm=%.6f sync=%.2fs",
            round_id,
            " (partial)" if partial else "",
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

    def _registration_monitor(self) -> None:
        timeout = float(self.config.coordinator.registration_timeout_s)
        min_workers = int(self.config.coordinator.min_workers)
        deadline = time.time() + timeout if timeout > 0 else None
        with self.cond:
            while not self.finished and self.registration_open and len(self.registered) < min_workers:
                if deadline is not None and time.time() >= deadline:
                    self.failure = (
                        f"registration timed out waiting for {min_workers} workers "
                        f"({len(self.registered)} registered)"
                    )
                    self.finished = True
                    self.cond.notify_all()
                    return
                wait = 0.5
                if deadline is not None:
                    wait = min(wait, max(0.0, deadline - time.time()))
                self.cond.wait(timeout=wait)
            if not self.finished and len(self.registered) >= min_workers:
                self.registration_open = False
                self.last_activity = time.time()
                self.cond.notify_all()
                LOG.info("registration closed with %s workers", len(self.registered))

    # ----------------------------------------------------------------- status
    def health(self) -> Dict[str, Any]:
        with self.cond:
            waiting_nodes = self._waiting_nodes_locked()
            return {
                "ok": self.failure is None,
                "finished": bool(self.finished),
                "round": int(self.round_index),
                "max_rounds": int(self.config.coordinator.max_rounds),
                "registered": len(self.registered),
                "pending_updates": len(self.updates),
                "waiting_workers": len(waiting_nodes),
                "failure": self.failure,
            }

    def public_status(self) -> Dict[str, Any]:
        with self.cond:
            last = self.last_result or {}
            last_round = None
            if last:
                last_round = {
                    "round": int(last.get("round", 0)),
                    "new_round": int(last.get("new_round", 0)),
                    "global_step": int(last.get("global_step", 0)),
                    "train_loss": float(last["train_loss"]) if last.get("train_loss") is not None else None,
                    "delta_norm": float(last["delta_norm"]) if last.get("delta_norm") is not None else None,
                    "sync_seconds": float(last["sync_seconds"]) if last.get("sync_seconds") is not None else None,
                    "update_bytes_total": int(last["update_bytes_total"]) if last.get("update_bytes_total") is not None else None,
                    "update_blob_bytes_total": int(last["update_blob_bytes_total"]) if last.get("update_blob_bytes_total") is not None else None,
                    "state_blob_bytes": int(last["state_blob_bytes"]) if last.get("state_blob_bytes") is not None else None,
                    "nodes_count": len(last.get("nodes") or []),
                    "done": bool(last.get("done", False)),
                    "partial": bool(last.get("partial", False)),
                    "updates_received": int(last.get("updates_received", len(last.get("nodes") or []))),
                    "quorum": int(last.get("quorum", self.config.coordinator.quorum)),
                }
            # Sanitized recent history for public trends (no node IDs/keys/logs)
            pub_history = []
            for item in list(self.history)[-24:]:
                if not isinstance(item, dict):
                    continue
                pub_history.append({
                    "round": int(item.get("round", 0)),
                    "train_loss": float(item["train_loss"]) if item.get("train_loss") is not None else None,
                    "delta_norm": float(item["delta_norm"]) if item.get("delta_norm") is not None else None,
                    "sync_seconds": float(item["sync_seconds"]) if item.get("sync_seconds") is not None else None,
                    "update_bytes_total": int(item["update_bytes_total"]) if item.get("update_bytes_total") is not None else None,
                    "update_blob_bytes_total": int(item["update_blob_bytes_total"]) if item.get("update_blob_bytes_total") is not None else None,
                    "state_blob_bytes": int(item["state_blob_bytes"]) if item.get("state_blob_bytes") is not None else None,
                    "nodes_count": len(item.get("nodes") or []),
                    "partial": bool(item.get("partial", False)),
                })
            waiting_workers = max(0, len(self.registered) - len(self.updates))
            round_wait_seconds = self._round_wait_seconds_locked() if (not self.finished and len(self.updates) > 0 and len(self.updates) < int(self.config.coordinator.quorum)) else 0.0
            return {
                "ok": self.failure is None,
                "run_id": self.config.run_id,
                "round": int(self.round_index),
                "max_rounds": int(self.config.coordinator.max_rounds),
                "global_step": int(self.global_step),
                "finished": bool(self.finished),
                "has_failure": self.failure is not None,
                "workers_active": len(self.registered),
                "pending_updates": len(self.updates),
                "waiting_workers": int(waiting_workers),
                "quorum": int(self.config.coordinator.quorum),
                "min_workers": int(self.config.coordinator.min_workers),
                "round_wait_seconds": float(round_wait_seconds),
                "effective_round_timeout_s": float(self._effective_round_timeout_locked()),
                "allow_partial_round": bool(self.config.coordinator.allow_partial_round),
                "last_round": last_round,
                "history": pub_history,
                "updated_at": time.time(),
            }

    def status(self) -> Dict[str, Any]:
        with self.cond:
            waiting_nodes = self._waiting_nodes_locked()
            return {
                "run_id": self.config.run_id,
                "round": int(self.round_index),
                "global_step": int(self.global_step),
                "max_rounds": int(self.config.coordinator.max_rounds),
                "finished": bool(self.finished),
                "failure": self.failure,
                "registered": sorted(self.registered),
                "pending_updates": sorted(self.updates),
                "waiting_nodes": waiting_nodes,
                "waiting_workers": len(waiting_nodes),
                "quorum": int(self.config.coordinator.quorum),
                "min_workers": int(self.config.coordinator.min_workers),
                "round_wait_seconds": float(self._round_wait_seconds_locked() if (not self.finished and self.round_started_at is not None and len(self.updates) < int(self.config.coordinator.quorum)) else 0.0),
                "round_timeout_s": float(self.config.coordinator.round_timeout_s),
                "effective_round_timeout_s": float(self._effective_round_timeout_locked()),
                "round_timeout_adaptive": bool(self.config.coordinator.round_timeout_adaptive),
                "adaptive_round_timeout_ema_s": float(self._round_timeout_ema) if self._round_timeout_ema is not None else None,
                "allow_partial_round": bool(self.config.coordinator.allow_partial_round),
                "partial_round_min_updates": int(self.config.coordinator.partial_round_min_updates),
                "idle_timeout_s": float(self.config.coordinator.idle_timeout_s),
                "registration_open": bool(self.registration_open),
                "last_result": self.last_result,
                "history": list(self.history),
                "state_hash": self._state_hash,
                "base_hash": self.base_hash,
                "config_hash": self.config.fingerprint(),
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
