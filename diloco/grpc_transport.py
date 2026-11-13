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
from .protocol import unpack_state
from .tensor_utils import clone_state, ensure_finite


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
            return pb2.StateResponse(payload=blob)

        def Submit(self, request: Any, context: Any) -> Any:
            self._authorize(context)
            try:
                meta, tensors = unpack_state(request.payload)
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
                    update_hash="grpc",
                    node_sig=str(meta.get("node_sig", "") or ""),
                    update_bytes=len(request.payload),
                )
            except Exception as exc:  # noqa: BLE001
                from .coordinator import ApiError

                if isinstance(exc, ApiError):
                    _abort(context, exc)
                    return
                raise
            except ValueError as exc:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            return pb2.StateResponse(payload=result)

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
        ) -> None:
            self.target = target
            self.token = token
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

        def _metadata(self) -> tuple[tuple[str, str], ...] | None:
            if not self.token:
                return None
            return (("x-diloco-token", self.token),)

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

        def get_state(self, node_id: str) -> bytes:
            response = self.stub.GetState(pb2.StateRequest(node_id=node_id), metadata=self._metadata())
            return response.payload

        def submit(self, payload: bytes) -> bytes:
            try:
                response = self.stub.Submit(pb2.SubmitRequest(payload=payload), metadata=self._metadata())
            except grpc.RpcError as exc:
                from .worker import CoordinatorError

                status = {
                    grpc.StatusCode.INVALID_ARGUMENT: 400,
                    grpc.StatusCode.UNAUTHENTICATED: 401,
                    grpc.StatusCode.PERMISSION_DENIED: 403,
                    grpc.StatusCode.NOT_FOUND: 404,
                    grpc.StatusCode.FAILED_PRECONDITION: 409,
                    grpc.StatusCode.RESOURCE_EXHAUSTED: 429,
                    grpc.StatusCode.INTERNAL: 500,
                    grpc.StatusCode.UNAVAILABLE: 503,
                    grpc.StatusCode.DEADLINE_EXCEEDED: 504,
                }.get(exc.code(), 500)
                raise CoordinatorError(status, exc.details() or str(exc)) from exc
            return response.payload

        def leave(self, node_id: str) -> Dict[str, Any]:
            try:
                response = self.stub.Leave(pb2.LeaveRequest(node_id=node_id), metadata=self._metadata())
            except grpc.RpcError as exc:
                from .worker import CoordinatorError

                status = {
                    grpc.StatusCode.INVALID_ARGUMENT: 400,
                    grpc.StatusCode.UNAUTHENTICATED: 401,
                    grpc.StatusCode.PERMISSION_DENIED: 403,
                    grpc.StatusCode.NOT_FOUND: 404,
                    grpc.StatusCode.FAILED_PRECONDITION: 409,
                    grpc.StatusCode.RESOURCE_EXHAUSTED: 429,
                    grpc.StatusCode.INTERNAL: 500,
                    grpc.StatusCode.UNAVAILABLE: 503,
                    grpc.StatusCode.DEADLINE_EXCEEDED: 504,
                }.get(exc.code(), 500)
                raise CoordinatorError(status, exc.details() or str(exc)) from exc
            return {"ok": bool(response.ok), "removed": bool(response.removed)}

        def close(self) -> None:
            self.channel.close()
