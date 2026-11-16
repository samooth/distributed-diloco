"""Optional gRPC transport for the DiLoCo coordinator.

The HTTP implementation remains the default and fallback. This module is only
imported when gRPC is requested, so the rest of the package keeps working
without grpcio.
"""
from __future__ import annotations

import hmac
import json
import ssl
from concurrent import futures
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

try:
    import grpc
    from . import diloco_pb2 as pb2
    from . import diloco_pb2_grpc as pb2_grpc
except ImportError:
    grpc = None
    pb2 = None
    pb2_grpc = None

from .compression import decompress_state
from .config import ExperimentConfig
from .protocol import hash_blob, unpack_state
from .tensor_utils import clone_state, ensure_finite
from .wire import available_wire_codecs, compress_wire, decompress_wire, negotiate_wire_codec, normalize_codec, parse_accept_wire


def _grpc_status(http_status: int) -> "grpc.StatusCode":
    mapping = {
        400: grpc.StatusCode.INVALID_ARGUMENT,
        401: grpc.StatusCode.UNAUTHENTICATED,
        403: grpc.StatusCode.PERMISSION_DENIED,
        404: grpc.StatusCode.NOT_FOUND,
        409: grpc.StatusCode.FAILED_PRECONDITION,
        410: grpc.StatusCode.NOT_FOUND,
        411: grpc.StatusCode.INVALID_ARGUMENT,
        413: grpc.StatusCode.RESOURCE_EXHAUSTED,
        416: grpc.StatusCode.OUT_OF_RANGE,
        425: grpc.StatusCode.FAILED_PRECONDITION,
        429: grpc.StatusCode.RESOURCE_EXHAUSTED,
        500: grpc.StatusCode.INTERNAL,
        503: grpc.StatusCode.UNAVAILABLE,
        504: grpc.StatusCode.DEADLINE_EXCEEDED,
    }
    return mapping.get(int(http_status), grpc.StatusCode.UNKNOWN)


def _abort(context: Any, exc: Any) -> None:
    context.abort(_grpc_status(exc.status), exc.message)


def _metadata(context: Any) -> Dict[str, str]:
    return {str(key).lower(): str(value) for key, value in context.invocation_metadata()}


if grpc is None:
    class _Servicer:  # type: ignore[no-redef]
        pass

    class GrpcCoordinatorServer:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise RuntimeError("grpcio is required for transport='grpc'")

    class GrpcWorkerClient:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            raise RuntimeError("grpcio is required for transport='grpc'")
else:
    class _Servicer(pb2_grpc.CoordinatorServicer):
        def __init__(self, coordinator: Coordinator) -> None:
            self.coordinator = coordinator

        def _authorize(self, context: Any) -> None:
            expected = self.coordinator.config.coordinator.token
            if not expected:
                return
            received = _metadata(context).get("x-diloco-token", "")
            if not hmac.compare_digest(str(received), str(expected)):
                context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid coordinator token")

        def _response_wire_codec(self, context: Any) -> str:
            pref = str(self.coordinator.config.coordinator.wire_compression or "none")
            if not pref or pref == "none":
                return ""
            md = _metadata(context)
            accepted = parse_accept_wire(md.get("x-diloco-accept-wire-compression", ""))
            return negotiate_wire_codec(pref, accepted)

        def _request_wire_codec(self, context: Any) -> str:
            md = _metadata(context)
            return normalize_codec(md.get("x-diloco-wire-compression", ""))

        def _compress_response(self, data: bytes, context: Any, codec: str = "") -> bytes:
            codec = codec or self._response_wire_codec(context)
            if not codec or not data:
                return data
            try:
                wire = compress_wire(data, codec)
            except Exception:
                return data
            try:
                context.send_initial_metadata((
                    ("x-diloco-wire-compression", codec),
                    ("x-diloco-wire-uncompressed-size", str(len(data))),
                    ("x-diloco-wire-bytes", str(len(wire))),
                ))
            except Exception:
                # metadata may already have been sent; fall back to uncompressed
                return data
            return wire

        def _decompress_request_payload(self, payload: bytes, context: Any) -> tuple[bytes, int]:
            wire_bytes = len(payload)
            codec = self._request_wire_codec(context)
            if not codec:
                return payload, wire_bytes
            try:
                return decompress_wire(payload, codec), wire_bytes
            except Exception as exc:  # noqa: BLE001
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"invalid wire-compressed payload: {exc}")

        def Register(self, request: Any, context: Any) -> Any:
            self._authorize(context)
            try:
                result = self.coordinator.register(
                    node_id=str(request.node_id),
                    config_hash=str(request.config_hash),
                    base_hash=str(request.base_hash),
                    node_pubkey=str(getattr(request, "node_pubkey", "") or ""),
                    node_sig=str(getattr(request, "node_sig", "") or ""),
                    sig_alg=str(getattr(request, "sig_alg", "") or ""),
                    run_id=str(getattr(request, "run_id", "") or ""),
                )
            except Exception as exc:  # noqa: BLE001
                from .coordinator import ApiError

                if isinstance(exc, ApiError):
                    _abort(context, exc)
                    return
                raise
            return pb2.RegisterResponse(
                ok=bool(result.get("ok", True)),
                node_id=str(result.get("node_id", request.node_id)),
                round=int(result.get("round", 0)),
                global_step=int(result.get("global_step", 0)),
                h=int(result.get("H", self.coordinator.config.train.H)),
                max_rounds=int(result.get("max_rounds", self.coordinator.config.coordinator.max_rounds)),
                done=bool(result.get("done", False)),
                state_hash=str(result.get("state_hash", "")),
                registered=list(result.get("registered", [])),
            )

        def Heartbeat(self, request: Any, context: Any) -> Any:
            self._authorize(context)
            try:
                result = self.coordinator.heartbeat(str(request.node_id))
            except Exception as exc:  # noqa: BLE001
                from .coordinator import ApiError

                if isinstance(exc, ApiError):
                    _abort(context, exc)
                    return
                raise
            return pb2.HeartbeatResponse(
                ok=bool(result.get("ok", True)),
                round=int(result.get("round", self.coordinator.round_index)),
                pending_updates=int(result.get("pending_updates", 0)),
            )

        def GetState(self, request: Any, context: Any) -> Any:
            self._authorize(context)
            try:
                blob = self.coordinator.get_state(str(request.node_id))
            except Exception as exc:  # noqa: BLE001
                from .coordinator import ApiError

                if isinstance(exc, ApiError):
                    _abort(context, exc)
                    return
                raise
            wire_blob = self._compress_response(blob, context)
            return pb2.StateResponse(payload=wire_blob)

        def GetStateMeta(self, request: Any, context: Any) -> Any:
            self._authorize(context)
            try:
                meta = self.coordinator.get_state_meta(str(request.node_id))
            except Exception as exc:  # noqa: BLE001
                from .coordinator import ApiError

                if isinstance(exc, ApiError):
                    _abort(context, exc)
                    return
                raise
            return pb2.StateMetaResponse(json=json.dumps(meta, sort_keys=True, default=str))

        def GetStateChunk(self, request: Any, context: Any) -> Any:
            self._authorize(context)
            try:
                chunk = self.coordinator.get_state_chunk(
                    str(request.node_id),
                    int(getattr(request, "offset", 0) or 0),
                    int(getattr(request, "length", 0) or 0),
                )
            except Exception as exc:  # noqa: BLE001
                from .coordinator import ApiError

                if isinstance(exc, ApiError):
                    _abort(context, exc)
                    return
                raise
            data = bytes(chunk["data"])
            wire_data = self._compress_response(data, context)
            return pb2.StateChunkResponse(
                data=wire_data,
                offset=int(chunk["offset"]),
                total_size=int(chunk["total_size"]),
                blob_hash=str(chunk["blob_hash"]),
                state_hash=str(chunk["state_hash"]),
                round=int(chunk["round"]),
                eof=bool(chunk["eof"]),
            )

        def Submit(self, request: Any, context: Any) -> Any:
            self._authorize(context)
            raw_payload = bytes(request.payload)
            try:
                payload, wire_bytes = self._decompress_request_payload(raw_payload, context)
            except Exception:
                return
            try:
                meta, tensors = unpack_state(payload)
            except Exception as exc:  # noqa: BLE001
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"invalid message: {exc}")
            node_id = str(meta.get("node_id", ""))
            try:
                round_id = int(meta.get("round", -1))
            except (TypeError, ValueError):
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid round")
            if int(meta.get("round", -2)) != round_id:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "round mismatch between request and payload")
            compression = str(meta.get("compression", "none"))
            expected = self.coordinator.config.train.delta_compression
            if compression != expected:
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"compression mismatch: got {compression!r}, expected {expected!r}",
                )
            try:
                if compression == "int8":
                    tensors = decompress_state(tensors, "int8", 1.0)
                elif compression in ("topk", "int8_topk"):
                    tensors = decompress_state(
                        tensors,
                        compression,
                        float(self.coordinator.config.train.top_k_ratio),
                    )
                delta = clone_state(tensors)
                ensure_finite(delta)
                result = self.coordinator.submit(
                    node_id=node_id,
                    round_id=round_id,
                    delta=delta,
                    weight=float(meta.get("weight", 1.0)),
                    train_loss=float(meta.get("train_loss", 0.0)),
                    update_hash=hash_blob(payload),
                    node_sig=str(meta.get("node_sig", "") or ""),
                    update_bytes=int(wire_bytes),
                )
            except Exception as exc:  # noqa: BLE001
                from .coordinator import ApiError

                if isinstance(exc, ApiError):
                    _abort(context, exc)
                    return
                raise
            except ValueError as exc:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            wire_result = self._compress_response(result, context)
            return pb2.StateResponse(payload=wire_result)

        def Leave(self, request: Any, context: Any) -> Any:
            self._authorize(context)
            result = self.coordinator.leave(str(request.node_id))
            return pb2.LeaveResponse(ok=True, removed=bool(result.get("removed", False)))

        def Health(self, request: Any, context: Any) -> Any:
            return pb2.HealthResponse(json=json.dumps(self.coordinator.health(), sort_keys=True))

        def Status(self, request: Any, context: Any) -> Any:
            return pb2.StatusResponse(json=json.dumps(self.coordinator.status(), sort_keys=True, default=str))


    class GrpcCoordinatorServer:
        def __init__(self, coordinator: Any, host: str, port: int) -> None:
            self.coordinator = coordinator
            self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=32))
            pb2_grpc.add_CoordinatorServicer_to_server(_Servicer(coordinator), self.server)
            cfg = coordinator.config.coordinator
            if cfg.tls_cert or cfg.tls_key:
                certificate = Path(cfg.tls_cert).read_bytes() if cfg.tls_cert else None
                private_key = Path(cfg.tls_key).read_bytes() if cfg.tls_key else None
                root_certificates = None
                if cfg.tls_client_ca:
                    root_certificates = Path(cfg.tls_client_ca).read_bytes()
                require_client_auth = bool(cfg.tls_require_client_cert or cfg.tls_client_ca)
                credentials = grpc.ssl_server_credentials(
                    [(private_key, certificate)] if private_key and certificate else None,
                    root_certificates=root_certificates,
                    require_client_auth=require_client_auth,
                )
                self.port = self.server.add_secure_port(f"{host}:{port}", credentials)
            else:
                self.port = self.server.add_insecure_port(f"{host}:{port}")
            if self.port == 0:
                raise RuntimeError("failed to bind gRPC port")
            self.server.start()

        def stop(self) -> None:
            self.server.stop(5)


    class GrpcWorkerClient:
        def __init__(
            self,
            target: str,
            token: str,
            tls_cert: str = "",
            tls_key: str = "",
            tls_client_ca: str = "",
            tls_verify: bool = True,
            wire_compression: str = "none",
        ) -> None:
            self.target = target
            self.token = token
            self.wire_compression = str(wire_compression or "none").lower()
            if not tls_verify:
                self.channel = grpc.insecure_channel(target)
            elif tls_cert and tls_key:
                root_certificates = Path(tls_client_ca).read_bytes() if tls_client_ca else None
                credentials = grpc.ssl_channel_credentials(
                    root_certificates=root_certificates,
                    private_key=Path(tls_key).read_bytes(),
                    certificate_chain=Path(tls_cert).read_bytes(),
                )
                self.channel = grpc.secure_channel(target, credentials)
            elif tls_client_ca:
                credentials = grpc.ssl_channel_credentials(root_certificates=Path(tls_client_ca).read_bytes())
                self.channel = grpc.secure_channel(target, credentials)
            else:
                self.channel = grpc.insecure_channel(target)
            self.stub = pb2_grpc.CoordinatorStub(self.channel)

        def _wire_accept(self) -> str:
            return ",".join(available_wire_codecs())

        def _submit_wire_codec(self) -> str:
            pref = normalize_codec(self.wire_compression)
            if not pref or pref == "none":
                return ""
            if pref == "auto":
                codecs = available_wire_codecs()
                return codecs[0] if codecs else ""
            return pref if pref in available_wire_codecs() else ""

        def _metadata(self, extra: Optional[Dict[str, str]] = None, include_accept: bool = True) -> tuple[tuple[str, str], ...] | None:
            md: list[tuple[str, str]] = []
            if self.token:
                md.append(("x-diloco-token", self.token))
            if include_accept:
                acc = self._wire_accept()
                if acc:
                    md.append(("x-diloco-accept-wire-compression", acc))
            if extra:
                for k, v in extra.items():
                    if v is None or v == "":
                        continue
                    md.append((str(k), str(v)))
            return tuple(md) if md else None

        def _response_meta(self, call: Any) -> Dict[str, str]:
            out: Dict[str, str] = {}
            try:
                for k, v in (call.initial_metadata() or []):
                    out[str(k).lower()] = str(v)
            except Exception:
                pass
            try:
                for k, v in (call.trailing_metadata() or []):
                    out[str(k).lower()] = str(v)
            except Exception:
                pass
            return out

        def _decompress_if_needed(self, payload: bytes, call: Any) -> tuple[bytes, int, int]:
            raw = bytes(payload)
            wire_bytes = len(raw)
            md = self._response_meta(call)
            codec = normalize_codec(md.get("x-diloco-wire-compression", ""))
            if codec:
                try:
                    out = decompress_wire(raw, codec)
                    return out, wire_bytes, len(out)
                except Exception:
                    return raw, wire_bytes, len(raw)
            return raw, wire_bytes, len(raw)

        def register(
            self,
            node_id: str,
            config_hash: str,
            base_hash: str,
            node_pubkey: str = "",
            node_sig: str = "",
            sig_alg: str = "",
            run_id: str = "",
            **_kwargs: Any,
        ) -> Dict[str, Any]:
            base = dict(node_id=node_id, config_hash=config_hash, base_hash=base_hash)
            extra = {}
            if node_pubkey:
                extra["node_pubkey"] = node_pubkey
            if node_sig:
                extra["node_sig"] = node_sig
            if sig_alg:
                extra["sig_alg"] = sig_alg
            if run_id:
                extra["run_id"] = run_id
            try:
                req = pb2.RegisterRequest(**base, **extra)
            except (TypeError, ValueError):
                req = pb2.RegisterRequest(**base)
            response = self.stub.Register(req, metadata=self._metadata())
            return {
                "ok": bool(response.ok),
                "node_id": str(response.node_id),
                "round": int(response.round),
                "global_step": int(response.global_step),
                "H": int(response.h),
                "max_rounds": int(response.max_rounds),
                "done": bool(response.done),
                "state_hash": str(response.state_hash),
                "registered": list(response.registered),
            }

        def heartbeat(self, node_id: str) -> Dict[str, Any]:
            response = self.stub.Heartbeat(
                pb2.HeartbeatRequest(node_id=node_id),
                metadata=self._metadata(),
            )
            return {
                "ok": bool(response.ok),
                "round": int(response.round),
                "pending_updates": int(response.pending_updates),
            }

        def _raise_rpc_error(self, exc: "grpc.RpcError") -> None:
            from .worker import CoordinatorError

            status = {
                grpc.StatusCode.INVALID_ARGUMENT: 400,
                grpc.StatusCode.UNAUTHENTICATED: 401,
                grpc.StatusCode.PERMISSION_DENIED: 403,
                grpc.StatusCode.NOT_FOUND: 404,
                grpc.StatusCode.FAILED_PRECONDITION: 409,
                grpc.StatusCode.OUT_OF_RANGE: 416,
                grpc.StatusCode.RESOURCE_EXHAUSTED: 429,
                grpc.StatusCode.INTERNAL: 500,
                grpc.StatusCode.UNAVAILABLE: 503,
                grpc.StatusCode.DEADLINE_EXCEEDED: 504,
            }.get(exc.code(), 500)
            raise CoordinatorError(status, exc.details() or str(exc)) from exc

        def get_state(self, node_id: str) -> tuple[bytes, int]:
            try:
                response, call = self.stub.GetState.with_call(
                    pb2.StateRequest(node_id=node_id),
                    metadata=self._metadata(),
                )
            except grpc.RpcError as exc:
                self._raise_rpc_error(exc)
            blob, wire_bytes, _ = self._decompress_if_needed(response.payload, call)
            return blob, int(wire_bytes)

        def get_state_meta(self, node_id: str) -> Dict[str, Any]:
            try:
                response = self.stub.GetStateMeta(
                    pb2.StateRequest(node_id=node_id),
                    metadata=self._metadata(include_accept=False),
                )
            except grpc.RpcError as exc:
                self._raise_rpc_error(exc)
            try:
                return json.loads(response.json)
            except Exception:
                return {}

        def get_state_chunk(self, node_id: str, offset: int = 0, length: int = 0) -> Dict[str, Any]:
            try:
                response, call = self.stub.GetStateChunk.with_call(
                    pb2.StateChunkRequest(node_id=node_id, offset=int(offset), length=int(length)),
                    metadata=self._metadata(),
                )
            except grpc.RpcError as exc:
                self._raise_rpc_error(exc)
            data, wire_bytes, _ = self._decompress_if_needed(response.data, call)
            return {
                "data": bytes(data),
                "offset": int(response.offset),
                "total_size": int(response.total_size),
                "blob_hash": str(response.blob_hash),
                "state_hash": str(response.state_hash),
                "round": int(response.round),
                "eof": bool(response.eof),
                "wire_bytes": int(wire_bytes),
            }

        def submit(self, payload: bytes) -> tuple[bytes, int, int]:
            codec = self._submit_wire_codec()
            wire_payload = bytes(payload)
            extra: Dict[str, str] = {}
            if codec:
                try:
                    wire_payload = compress_wire(wire_payload, codec)
                    extra["x-diloco-wire-compression"] = codec
                except Exception:
                    wire_payload = bytes(payload)
                    extra = {}
            req_wire = len(wire_payload)
            try:
                response, call = self.stub.Submit.with_call(
                    pb2.SubmitRequest(payload=wire_payload),
                    metadata=self._metadata(extra=extra or None),
                )
            except grpc.RpcError as exc:
                self._raise_rpc_error(exc)
            blob, resp_wire, _ = self._decompress_if_needed(response.payload, call)
            return blob, int(resp_wire), int(req_wire)

        def leave(self, node_id: str) -> Dict[str, Any]:
            try:
                response = self.stub.Leave(pb2.LeaveRequest(node_id=node_id), metadata=self._metadata())
            except grpc.RpcError as exc:
                self._raise_rpc_error(exc)
            return {"ok": bool(response.ok), "removed": bool(response.removed)}

        def close(self) -> None:
            self.channel.close()
