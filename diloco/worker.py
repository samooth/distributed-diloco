"""DiLoCo worker: local adapter steps + pseudo-gradient upload + global reload."""
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import torch

try:
    import grpc
except ImportError:  # pragma: no cover
    grpc = None

from .backend import AdapterBackend
try:
    from .grpc_transport import GrpcWorkerClient
except RuntimeError:  # grpcio is optional
    GrpcWorkerClient = None
from .compression import compress_state, decompress_state
from .checkpoint import load_initial_state
from .config import ExperimentConfig
from .protocol import hash_blob, pack_message, unpack_message
from .tensor_utils import clone_state, global_norm, hash_state, scale_state, subtract_state

LOG = logging.getLogger("diloco.worker")


class CoordinatorError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = int(status)


class Worker:
    def __init__(
        self,
        config: ExperimentConfig,
        node_id: str,
        coordinator_url: str,
        backend: AdapterBackend,
        log_path: Optional[str] = None,
        grpc_target: Optional[str] = None,
        init_state_path: Optional[str] = None,
        node_identity_path: Optional[str] = None,
        state_cache_dir: Optional[str] = None,
    ) -> None:
        self.config = config
        self.node_id = str(node_id)
        self.coordinator_url = coordinator_url.rstrip("/")
        self.backend = backend
        self.grpc_target = grpc_target
        self.init_state_path = Path(init_state_path) if init_state_path else None
        self.node_identity_path = Path(node_identity_path) if node_identity_path else None
        self.node_identity: Optional[Dict[str, Any]] = self._load_identity(self.node_identity_path)
        self.state_cache_dir = Path(state_cache_dir).expanduser().resolve() if state_cache_dir else (Path("runs") / "state-cache")
        self._grpc: Optional[GrpcWorkerClient] = None
        self.log_path = Path(log_path) if log_path else None
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._timeout = float(config.coordinator.round_timeout_s) + 60.0
        idle = float(config.coordinator.idle_timeout_s)
        self._heartbeat_interval = 30.0 if idle <= 0 else max(1.0, min(30.0, idle / 3.0))
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._residual: Optional[Dict[str, torch.Tensor]] = None

    @staticmethod
    def _load_identity(path: Optional[Path]) -> Optional[Dict[str, Any]]:
        if path is None:
            return None
        path = path.expanduser().resolve()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise CoordinatorError(0, f"cannot load node identity {path}: {exc}") from exc
        if not isinstance(data, dict) or not str(data.get("pubkey", "")).strip():
            raise CoordinatorError(0, f"invalid node identity file: {path}")
        return data

    def _headers(self) -> Dict[str, str]:
        token = self.config.coordinator.token
        return {"X-DiLoCo-Token": token} if token else {}

    def _ssl_context(self) -> Optional[Any]:
        if not self.coordinator_url.startswith("https://"):
            return None
        import ssl

        if self.config.coordinator.tls_cert and self.config.coordinator.tls_key:
            context = ssl.create_default_context()
            context.load_cert_chain(
                self.config.coordinator.tls_cert,
                self.config.coordinator.tls_key,
            )
        else:
            context = ssl.create_default_context()
        if self.config.coordinator.tls_client_ca:
            context.load_verify_locations(self.config.coordinator.tls_client_ca)
        if not self.config.coordinator.tls_verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        if self.config.coordinator.tls_require_client_cert is False:
            pass
        return context

    def _get_grpc_client(self) -> Optional[GrpcWorkerClient]:
        if self.config.coordinator.transport != "grpc":
            return None
        if GrpcWorkerClient is None:
            raise CoordinatorError(0, "grpcio is required for transport='grpc'")
        if self._grpc is None:
            target = self.grpc_target
            if not target:
                url = self.coordinator_url
                if url.startswith("grpc://"):
                    target = url[len("grpc://"):]
                elif url.startswith("http://"):
                    target = url[len("http://"):]
                elif url.startswith("https://"):
                    target = url[len("https://"):]
                else:
                    target = url
            if not target:
                raise CoordinatorError(0, "gRPC target is required")
            self._grpc = GrpcWorkerClient(
                target=target,
                token=self.config.coordinator.token,
                tls_cert=self.config.coordinator.tls_cert,
                tls_key=self.config.coordinator.tls_key,
                tls_client_ca=self.config.coordinator.tls_client_ca,
                tls_verify=self.config.coordinator.tls_verify,
            )
        return self._grpc

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        content_type: str = "application/json",
        timeout: Optional[float] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        allow_partial: bool = False,
        retry_statuses: Optional[Sequence[int]] = None,
    ) -> tuple[int, bytes, str, Dict[str, str]]:
        url = self.coordinator_url + path
        headers = dict(self._headers())
        if extra_headers:
            headers.update(extra_headers)
        if body is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        context = self._ssl_context()
        idempotent = method == "GET" or path in ("/v1/register", "/v1/heartbeat", "/v1/leave")
        attempts = 5 if idempotent else 1
        retryable_http = set(int(s) for s in (retry_statuses or ())) | ({502, 503, 504} if idempotent else set())
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self._timeout if timeout is None else timeout,
                    context=context,
                ) as response:
                    status = int(response.status)
                    resp_headers = {k: v for k, v in response.headers.items()}
                    try:
                        data = response.read()
                    except http.client.IncompleteRead as exc:
                        if allow_partial and exc.partial:
                            return status, bytes(exc.partial), response.headers.get_content_type() or "", resp_headers
                        raise
                    return status, data, response.headers.get_content_type() or "", resp_headers
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                message = raw.decode("utf-8", errors="replace")
                try:
                    payload = json.loads(message)
                    message = str(payload.get("error", message))
                except json.JSONDecodeError:
                    pass
                if attempt < attempts - 1 and int(exc.code) in retryable_http:
                    last_exc = CoordinatorError(exc.code, message)
                    LOG.warning("retryable HTTP %s on %s %s (attempt %s/%s): %s", exc.code, method, path, attempt + 1, attempts, message)
                    time.sleep(min(1.5 * (attempt + 1), 8.0))
                    continue
                raise CoordinatorError(exc.code, message) from exc
            except http.client.IncompleteRead as exc:
                last_exc = exc
                if allow_partial and exc.partial:
                    # Caller will decide how to resume
                    return 200, bytes(exc.partial), "", {}
                LOG.warning("incomplete read on %s %s (attempt %s/%s): %s bytes, retrying", method, path, attempt + 1, attempts, len(exc.partial))
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError, TimeoutError) as exc:
                last_exc = exc
                if attempts > 1:
                    LOG.warning("connection error on %s %s (attempt %s/%s): %s", method, path, attempt + 1, attempts, exc)
                else:
                    raise CoordinatorError(0, f"connection error: {exc}") from exc
            except urllib.error.URLError as exc:
                # URLError may wrap the above; retry for idempotent-ish requests
                last_exc = exc
                if attempts > 1:
                    LOG.warning("url error on %s %s (attempt %s/%s): %s", method, path, attempt + 1, attempts, exc.reason)
                else:
                    raise CoordinatorError(0, f"cannot reach coordinator: {exc.reason}") from exc
            if attempt < attempts - 1:
                time.sleep(min(2.0 * (attempt + 1), 8.0))
        raise CoordinatorError(0, f"request failed after {attempts} attempts: {last_exc}") from last_exc

    def _wait_for_round_result(self, submitted_round: int, timeout_s: float = 300.0) -> tuple[Dict[str, Any], Dict[str, torch.Tensor]]:
        """Wait until coordinator advances past submitted_round, then fetch new state.
        Used to recover from ambiguous/duplicate update submissions."""
        deadline = time.time() + max(10.0, float(timeout_s))
        last_err: Optional[Exception] = None
        while time.time() < deadline:
            try:
                meta = self.get_state_meta()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(2.0)
                continue
            if meta.get("failure"):
                raise CoordinatorError(503, f"run failed: {meta.get('failure')}")
            cur = int(meta.get("round", 0) or 0)
            if cur > int(submitted_round) or bool(meta.get("done")):
                # fetch state (idempotent; may reuse in-memory if matching)
                return self.get_state()
            time.sleep(min(2.0, max(0.2, deadline - time.time())))
        if last_err:
            raise CoordinatorError(0, f"timed out waiting for round {submitted_round} result: {last_err}") from last_err
        raise CoordinatorError(504, f"timed out waiting for round {submitted_round} to complete")

    def _download_state_blob_http(self, expected_meta: Optional[Mapping[str, Any]] = None) -> bytes:
        """Download /v1/state with Range-based resume, chunk/manifest verification and idempotent retries."""
        query = urllib.parse.urlencode({"node_id": self.node_id})
        path = f"/v1/state?{query}"
        expected_hash = str((expected_meta or {}).get("state_blob_hash", "") or "")
        expected_size = int((expected_meta or {}).get("state_blob_bytes", 0) or 0)
        expected_state_hash = str((expected_meta or {}).get("state_hash", "") or "")
        expected_round = int((expected_meta or {}).get("round", 0) or 0)
        manifest_chunk_size = int((expected_meta or {}).get("state_blob_chunk_size", 0) or 0)
        manifest_hashes = list((expected_meta or {}).get("state_blob_chunk_hashes") or [])
        merkle_root = str((expected_meta or {}).get("state_blob_merkle_root", "") or "")

        def _verify_manifest(blob: bytes) -> bool:
            if not expected_hash and not manifest_hashes:
                return True
            if expected_hash and hash_blob(blob) != expected_hash:
                return False
            if manifest_hashes and manifest_chunk_size > 0:
                got = []
                for i in range(0, len(blob), manifest_chunk_size):
                    got.append(hashlib.sha256(blob[i : i + manifest_chunk_size]).hexdigest())
                if got != list(manifest_hashes):
                    return False
            return True

        # Small/unknown blobs: single request (compatibility path)
        if expected_size <= 0 or (expected_size <= 8 * 1024 * 1024 and not manifest_hashes):
            _, raw, _, _ = self._request("GET", path)
            if not _verify_manifest(raw):
                raise CoordinatorError(0, "state blob hash/manifest mismatch")
            return raw

        chunk_size = manifest_chunk_size if manifest_chunk_size > 0 else 8 * 1024 * 1024
        if chunk_size <= 0:
            chunk_size = 8 * 1024 * 1024

        def _state_changed(headers: Mapping[str, str]) -> bool:
            h_blob = str(headers.get("X-DiLoCo-Blob-Hash", "") or "")
            h_state = str(headers.get("X-DiLoCo-State-Hash", "") or "")
            h_round = str(headers.get("X-DiLoCo-Round", "") or "")
            if expected_hash and h_blob and h_blob != expected_hash:
                return True
            if expected_state_hash and h_state and h_state != expected_state_hash:
                return True
            # allow round to move forward only if hash also matches expectation logic; safest: require same expected identity when provided
            if expected_round and h_round and str(expected_round) != h_round and (expected_hash or expected_state_hash):
                # if blob/state hashes are consistent with expected, round header alone is not authoritative; otherwise treat as changed
                if not ((expected_hash and h_blob == expected_hash) or (expected_state_hash and h_state == expected_state_hash)):
                    return True
            return False

        buf = bytearray()
        offset = 0
        attempts = 0
        max_attempts = 30
        restart_count = 0
        while offset < expected_size and attempts < max_attempts:
            end = min(expected_size - 1, offset + chunk_size - 1)
            expected_chunk_index = offset // chunk_size if chunk_size > 0 else 0
            try:
                status, data, _, resp_headers = self._request(
                    "GET",
                    path,
                    extra_headers={"Range": f"bytes={offset}-{end}"},
                    allow_partial=True,
                )
            except CoordinatorError as exc:
                attempts += 1
                if exc.status == 416:
                    if restart_count < 2:
                        LOG.warning("%s range not satisfiable at offset %s, restarting state download", self.node_id, offset)
                        buf.clear()
                        offset = 0
                        restart_count += 1
                        continue
                LOG.warning("%s state chunk download failed (attempt %s): %s", self.node_id, attempts, exc)
                time.sleep(min(1.5 * attempts, 8.0))
                continue
            except Exception as exc:  # noqa: BLE001
                attempts += 1
                LOG.warning("%s state chunk download error (attempt %s): %s", self.node_id, attempts, exc)
                time.sleep(min(1.5 * attempts, 8.0))
                continue

            if status == 200:
                # Server ignored Range or returned full blob
                if data and offset == 0:
                    if not _verify_manifest(data):
                        attempts += 1
                        LOG.warning("%s full state blob/manifest mismatch (attempt %s)", self.node_id, attempts)
                        continue
                    return data
                attempts += 1
                continue

            if status != 206 or not data:
                attempts += 1
                continue

            if _state_changed(resp_headers):
                if restart_count < 3:
                    LOG.warning("%s coordinator state changed during download, restarting", self.node_id)
                    buf.clear()
                    offset = 0
                    restart_count += 1
                    # refresh expected identity from latest metadata when possible
                    try:
                        fresh = self.get_state_meta()
                        expected_hash = str(fresh.get("state_blob_hash", expected_hash) or expected_hash)
                        expected_size = int(fresh.get("state_blob_bytes", expected_size) or expected_size)
                        expected_state_hash = str(fresh.get("state_hash", expected_state_hash) or expected_state_hash)
                        expected_round = int(fresh.get("round", expected_round) or expected_round)
                        manifest_chunk_size = int(fresh.get("state_blob_chunk_size", manifest_chunk_size) or manifest_chunk_size)
                        manifest_hashes = list(fresh.get("state_blob_chunk_hashes") or manifest_hashes)
                        if manifest_chunk_size > 0:
                            chunk_size = manifest_chunk_size
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                raise CoordinatorError(0, "coordinator state kept changing during download")

            # Accept partial data; accumulate until we have this chunk complete (or final tail)
            chunk_end = min(expected_size, offset - (offset % chunk_size) + chunk_size) if chunk_size > 0 else expected_size
            # But we requested from `offset` to `end`; append what we got
            buf.extend(data)
            new_offset = offset + len(data)
            # Verify completed manifest chunks
            if manifest_hashes and manifest_chunk_size > 0:
                verified_chunks = len(buf) // manifest_chunk_size
                # verify the chunk(s) that just became complete
                start_chunk = offset // manifest_chunk_size
                end_chunk_complete = min(verified_chunks, len(manifest_hashes))
                for idx in range(start_chunk, end_chunk_complete):
                    cstart = idx * manifest_chunk_size
                    cend = min(expected_size, cstart + manifest_chunk_size)
                    if len(buf) < cend:
                        break
                    chash = hashlib.sha256(bytes(buf[cstart:cend])).hexdigest()
                    expected_chash = str(manifest_hashes[idx])
                    if chash != expected_chash:
                        # discard this chunk and retry from its start
                        LOG.warning("%s chunk hash mismatch at index %s, retrying chunk", self.node_id, idx)
                        del buf[cstart:]
                        new_offset = cstart
                        attempts += 1
                        data = b""  # mark no progress in terms of valid bytes
                        break
            offset = new_offset
            if len(data) > 0 and offset > (new_offset - len(data)):
                attempts = 0  # progress made

            if offset >= expected_size:
                break

        if offset < expected_size:
            raise CoordinatorError(0, f"incomplete state download ({offset}/{expected_size} bytes)")
        blob = bytes(buf[:expected_size])
        if not _verify_manifest(blob):
            raise CoordinatorError(0, "state blob hash/manifest mismatch after resumable download")
        return blob

    def register(self) -> Dict[str, Any]:
        pubkey = str((self.node_identity or {}).get("pubkey", "")).strip()
        body_dict = {
            "node_id": self.node_id,
            "config_hash": self.config.fingerprint(),
            "base_hash": self.backend.base_fingerprint(),
            "run_id": self.config.run_id,
        }
        if pubkey:
            body_dict["node_pubkey"] = pubkey
        if self.node_identity_path and pubkey:
            try:
                from .identity import canonical_register, sign_message
                msg = canonical_register(self.node_id, self.config.fingerprint(), self.backend.base_fingerprint(), self.config.run_id)
                sig = sign_message(self.node_identity_path, msg)
                body_dict["node_sig"] = sig.get("sig", "")
                body_dict["sig_alg"] = sig.get("alg", "")
            except Exception as exc:  # noqa: BLE001
                LOG.warning("%s could not sign registration: %s", self.node_id, exc)

        client = self._get_grpc_client()
        if client is not None:
            try:
                return client.register(
                    node_id=self.node_id,
                    config_hash=self.config.fingerprint(),
                    base_hash=self.backend.base_fingerprint(),
                    node_pubkey=pubkey,
                    node_sig=str(body_dict.get("node_sig", "")),
                    sig_alg=str(body_dict.get("sig_alg", "")),
                    run_id=self.config.run_id,
                )
            except TypeError:
                # Backward compat with older gRPC clients
                try:
                    return client.register(
                        node_id=self.node_id,
                        config_hash=self.config.fingerprint(),
                        base_hash=self.backend.base_fingerprint(),
                        node_pubkey=pubkey,
                    )
                except TypeError:
                    return client.register(
                        node_id=self.node_id,
                        config_hash=self.config.fingerprint(),
                        base_hash=self.backend.base_fingerprint(),
                    )
        body = json.dumps(body_dict).encode("utf-8")
        status, raw, _, _ = self._request("POST", "/v1/register", body)
        payload = json.loads(raw.decode("utf-8"))
        if status != 200 or not payload.get("ok"):
            raise CoordinatorError(status, f"registration rejected: {payload}")
        return payload

    def _start_heartbeat(self) -> None:
        if self._heartbeat_thread is not None:
            return
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"diloco-heartbeat-{self.node_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._heartbeat_thread = None

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self._heartbeat_interval):
            query = urllib.parse.urlencode({"node_id": self.node_id})
            client = self._get_grpc_client()
            if client is not None:
                try:
                    client.heartbeat(self.node_id)
                except Exception as exc:  # noqa: BLE001
                    if isinstance(exc, CoordinatorError) and exc.status in (403, 410):
                        return
                    LOG.debug("%s heartbeat failed: %s", self.node_id, exc)
            else:
                try:
                    self._request("GET", f"/v1/heartbeat?{query}", timeout=10.0)
                except CoordinatorError as exc:
                    if exc.status in (403, 410):
                        return
                    LOG.debug("%s heartbeat failed: %s", self.node_id, exc)
                except Exception as exc:  # noqa: BLE001
                    LOG.debug("%s heartbeat failed: %s", self.node_id, exc)

    def get_state_meta(self) -> Dict[str, Any]:
        client = self._get_grpc_client()
        if client is not None and hasattr(client, "get_state_meta"):
            return client.get_state_meta(self.node_id)
        query = urllib.parse.urlencode({"node_id": self.node_id})
        try:
            _, raw, _, _ = self._request("GET", f"/v1/state_meta?{query}")
            return json.loads(raw.decode("utf-8"))
        except CoordinatorError as exc:
            if exc.status != 404:
                raise
        # Fallback for older coordinators: use /v1/status (small)
        _, raw, _, _ = self._request("GET", "/v1/status")
        status = json.loads(raw.decode("utf-8"))
        return {
            "round": int(status.get("round", 0)),
            "global_step": int(status.get("global_step", 0)),
            "H": int(self.config.train.H),
            "max_rounds": int(status.get("max_rounds", self.config.coordinator.max_rounds)),
            "done": bool(status.get("finished", False)),
            "state_hash": str(status.get("state_hash", "")),
            "base_hash": str(status.get("base_hash", "")),
            "config_hash": self.config.fingerprint(),
            "adapter_meta": status.get("adapter_meta", {}),
        }

    def _state_cache_path(self, state_hash: str) -> Path:
        safe = "".join(c for c in str(state_hash) if c.isalnum() or c in "-_")[:128] or "state"
        return self.state_cache_dir / f"{safe}.blob"

    def _load_cached_state_blob(self, expected_meta: Mapping[str, Any]) -> Optional[bytes]:
        state_hash = str(expected_meta.get("state_hash", "") or "")
        if not state_hash:
            return None
        path = self._state_cache_path(state_hash)
        if not path.is_file():
            return None
        try:
            blob = path.read_bytes()
        except Exception:  # noqa: BLE001
            return None
        expected_blob_hash = str(expected_meta.get("state_blob_hash", "") or "")
        if expected_blob_hash and hash_blob(blob) != expected_blob_hash:
            try:
                path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            return None
        try:
            meta, state = unpack_message(blob)
        except Exception:  # noqa: BLE001
            try:
                path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            return None
        if str(meta.get("config_hash", "")) != self.config.fingerprint():
            return None
        if str(meta.get("base_hash", "")) != self.backend.base_fingerprint():
            return None
        if str(meta.get("state_hash", "")) != state_hash or hash_state(state) != state_hash:
            return None
        chunk_hashes = list(expected_meta.get("state_blob_chunk_hashes") or [])
        chunk_size = int(expected_meta.get("state_blob_chunk_size", 0) or 0)
        if chunk_hashes and chunk_size > 0:
            got = [hashlib.sha256(blob[i:i + chunk_size]).hexdigest() for i in range(0, len(blob), chunk_size)]
            if got != [str(h) for h in chunk_hashes]:
                return None
        LOG.info("%s loaded state from local cache (%s)", self.node_id, state_hash[:12])
        return blob

    def _save_cached_state_blob(self, state_hash: str, blob: bytes) -> None:
        if not state_hash or not blob:
            return
        try:
            self.state_cache_dir.mkdir(parents=True, exist_ok=True)
            path = self._state_cache_path(state_hash)
            tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
            tmp.write_bytes(blob)
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("%s could not save state cache (%s): %s", self.node_id, str(state_hash)[:12], exc)

    def _load_local_init_state(self) -> Optional[tuple[Dict[str, Any], Dict[str, torch.Tensor]]]:
        if not self.init_state_path:
            return None
        try:
            remote = self.get_state_meta()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("%s could not fetch state_meta for local bootstrap: %s", self.node_id, exc)
            return None
        try:
            state, base_hash, adapter_meta, bundle_meta = load_initial_state(self.init_state_path)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("%s could not load local init state %s: %s", self.node_id, self.init_state_path, exc)
            return None
        if str(remote.get("base_hash", "")) != str(base_hash):
            return None
        if str(remote.get("config_hash", "")) != self.config.fingerprint():
            return None
        if str(remote.get("state_hash", "")) != str(bundle_meta.get("state_hash", "")):
            LOG.info("%s local init state hash does not match coordinator (round %s); downloading state", self.node_id, remote.get("round"))
            return None
        if str(bundle_meta.get("config_hash", "")) != self.config.fingerprint():
            return None
        self._validate_state(remote, state)
        LOG.info("%s bootstrapping from local init state (round %s, hash %s)", self.node_id, remote.get("round"), remote.get("state_hash"))
        return dict(remote), clone_state(state)

    def _current_state_matches_meta(self, remote: Mapping[str, Any]) -> bool:
        try:
            if str(remote.get("base_hash", "")) != self.backend.base_fingerprint():
                return False
            if str(remote.get("config_hash", "")) != self.config.fingerprint():
                return False
            expected_state_hash = str(remote.get("state_hash", "") or "")
            if not expected_state_hash:
                return False
            current = self.backend.state()
            return hash_state(current) == expected_state_hash
        except Exception:  # noqa: BLE001
            return False

    def get_state(self, download: bool = False) -> tuple[Dict[str, Any], Dict[str, torch.Tensor]]:
        if not download:
            local = self._load_local_init_state()
            if local is not None:
                return local

        client = self._get_grpc_client()
        if client is not None:
            # If server exposes state_meta, reuse in-memory or disk-cached state when identical
            expected_meta: Optional[Dict[str, Any]] = None
            if hasattr(client, "get_state_meta"):
                try:
                    expected_meta = client.get_state_meta(self.node_id)
                    if self._current_state_matches_meta(expected_meta):
                        state = self.backend.state()
                        return dict(expected_meta), clone_state(state)
                except Exception:  # noqa: BLE001
                    expected_meta = None
            if expected_meta:
                cached = self._load_cached_state_blob(expected_meta)
                if cached is not None:
                    meta, state = unpack_message(cached)
                    self._validate_state(meta, state)
                    return meta, clone_state(state)
            raw = client.get_state(self.node_id)
            meta, state = unpack_message(raw)
            self._validate_state(meta, state)
            state_hash = str(meta.get("state_hash", "") or "")
            if state_hash:
                self._save_cached_state_blob(state_hash, raw)
            return meta, clone_state(state)

        # HTTP: try resumable/chunked download when blob metadata is available
        expected_meta: Optional[Dict[str, Any]] = None
        try:
            expected_meta = self.get_state_meta()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("%s could not fetch state_meta before download: %s", self.node_id, exc)

        # Idempotency: if current in-memory state already matches coordinator state, skip download
        if expected_meta and self._current_state_matches_meta(expected_meta):
            LOG.info(
                "%s reusing in-memory state (round %s, hash %s)",
                self.node_id,
                expected_meta.get("round"),
                expected_meta.get("state_hash"),
            )
            return dict(expected_meta), clone_state(self.backend.state())

        # Persistent cache: reuse downloaded blob for this state_hash if available
        if expected_meta:
            cached = self._load_cached_state_blob(expected_meta)
            if cached is not None:
                meta, state = unpack_message(cached)
                self._validate_state(meta, state)
                return meta, clone_state(state)

        raw = self._download_state_blob_http(expected_meta)
        meta, state = unpack_message(raw)
        self._validate_state(meta, state)
        state_hash = str(meta.get("state_hash", "") or "")
        if state_hash:
            self._save_cached_state_blob(state_hash, raw)
        return meta, clone_state(state)

    def _validate_state(
        self,
        meta: Mapping[str, Any],
        state: Mapping[str, torch.Tensor],
    ) -> None:
        if str(meta.get("base_hash", "")) != self.backend.base_fingerprint():
            raise CoordinatorError(0, "coordinator returned a different base model")
        if str(meta.get("config_hash", "")) != self.config.fingerprint():
            raise CoordinatorError(0, "coordinator returned a different config")
        actual = hash_state(state)
        if str(meta.get("state_hash", "")) != actual:
            raise CoordinatorError(0, "global state hash mismatch")

    def _append_log(self, record: Mapping[str, Any]) -> None:
        if self.log_path is None:
            return
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(record), sort_keys=True, default=str) + "\n")

    def run(self) -> Dict[str, Any]:
        try:
            info = self.register()
        except CoordinatorError as exc:
            self._append_log(
                {
                    "node_id": self.node_id,
                    "error": str(exc),
                    "status": exc.status,
                }
            )
            LOG.warning("%s: %s", self.node_id, exc)
            return {
                "node_id": self.node_id,
                "rounds_done": 0,
                "start_round": 0,
                "final_round": 0,
                "initial_eval_loss": None,
                "final_eval_loss": None,
                "registered_at_start": [],
                "last_metrics": {},
                "failure": str(exc),
            }
        self._start_heartbeat()
        try:
            return self._run_registered(info)
        finally:
            self._stop_heartbeat()

    def _run_registered(self, info: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            meta, state = self.get_state()
        except CoordinatorError as exc:
            if exc.status == 403:
                try:
                    self.register()
                except CoordinatorError:
                    pass
                meta, state = self.get_state()
            else:
                raise
        start_round = int(meta["round"])
        max_rounds = int(meta["max_rounds"])
        LOG.info(
            "%s registered; starting at round %s/%s",
            self.node_id,
            start_round,
            max_rounds,
        )

        self.backend.load_state(clone_state(state))
        initial_eval = self.backend.evaluate()
        rounds_done = 0
        stale_retries = 0
        run_failed = False
        last_metrics: Dict[str, Any] = {}
        while int(meta["round"]) < max_rounds:
            round_id = int(meta["round"])
            global_state = clone_state(state)
            self.backend.load_state(global_state)
            if self.config.train.reset_inner_optimizer:
                self.backend.reset_optimizer()
            should_eval = (
                self.config.data.eval_every > 0
                and round_id % int(self.config.data.eval_every) == 0
            )
            eval_before = self.backend.evaluate().get("eval_loss") if should_eval else None
            started = time.time()
            stats = self.backend.train_steps(
                int(self.config.train.H),
                round_index=round_id,
            )
            train_seconds = time.time() - started
            local_state = self.backend.state()
            delta = subtract_state(global_state, local_state)
            raw_norm = global_norm(delta)
            if self.config.train.max_delta_norm > 0 and raw_norm > self.config.train.max_delta_norm:
                delta = scale_state(delta, self.config.train.max_delta_norm / raw_norm)
            delta_norm = global_norm(delta)
            eval_after = self.backend.evaluate().get("eval_loss") if should_eval else None
            weight = float(stats.get("weight", stats.get("tokens", stats.get("samples", 1.0))))
            if weight <= 0:
                weight = float(max(1, self.config.train.batch_size))
            compression = self.config.train.delta_compression
            top_k_ratio = float(self.config.train.top_k_ratio)
            error_feedback = bool(self.config.train.error_feedback)
            if error_feedback:
                if self._residual is None:
                    self._residual = {
                        key: torch.zeros_like(value, device="cpu", dtype=torch.float32)
                        for key, value in delta.items()
                    }
                upload_input = {
                    key: self._residual[key] + delta[key] for key in delta
                }
            else:
                upload_input = delta
            upload_state = compress_state(upload_input, compression, top_k_ratio)
            reconstructed = decompress_state(upload_state, compression, top_k_ratio)
            if error_feedback and self._residual is not None:
                self._residual = {
                    key: self._residual[key] - reconstructed[key] for key in reconstructed
                }
            payload_meta = {
                "node_id": self.node_id,
                "round": round_id,
                "weight": weight,
                "train_loss": float(stats.get("loss", 0.0)),
                "delta_norm": delta_norm,
                "eval_before": eval_before,
                "eval_after": eval_after,
                "train_seconds": train_seconds,
                "compression": compression,
            }
            pubkey = str((self.node_identity or {}).get("pubkey", "")).strip()
            if self.node_identity_path and pubkey:
                try:
                    from .identity import canonical_update, sign_message
                    msg = canonical_update(
                        self.node_id,
                        round_id,
                        "",
                        self.config.fingerprint(),
                        str(meta.get("state_hash", "")),
                    )
                    sig = sign_message(self.node_identity_path, msg)
                    payload_meta["node_pubkey"] = pubkey
                    payload_meta["node_sig"] = sig.get("sig", "")
                    payload_meta["sig_alg"] = sig.get("alg", "")
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("%s could not sign update: %s", self.node_id, exc)
            client = self._get_grpc_client()
            if client is not None:
                blob = pack_message(payload_meta, upload_state)
                try:
                    raw = client.submit(blob)
                except Exception as exc:  # noqa: BLE001
                    if grpc is not None and isinstance(exc, grpc.RpcError):
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
                        message = exc.details() or str(exc)
                    elif isinstance(exc, CoordinatorError):
                        status = exc.status
                        message = str(exc)
                    else:
                        status = 500
                        message = str(exc)
                    mapped = CoordinatorError(status, message)
                    if mapped.status == 409 and "stale round" in str(mapped) and stale_retries < 3:
                        stale_retries += 1
                        self._append_log(
                            {
                                "node_id": self.node_id,
                                "round": round_id,
                                "stale_round_retry": stale_retries,
                                "error": str(mapped),
                            }
                        )
                        LOG.warning(
                            "%s round %s became stale; reloading global state (retry %s/3)",
                            self.node_id,
                            round_id,
                            stale_retries,
                        )
                        new_meta, new_state = self.get_state()
                        if int(new_meta["round"]) <= round_id:
                            raise mapped
                        self._residual = None
                        meta, state = new_meta, clone_state(new_state)
                        continue
                    if mapped.status == 403 and stale_retries < 3:
                        stale_retries += 1
                        try:
                            self.register()
                        except CoordinatorError:
                            pass
                        try:
                            new_meta, new_state = self.get_state()
                        except CoordinatorError:
                            raise mapped
                        self._residual = None
                        meta, state = new_meta, clone_state(new_state)
                        continue
                    if mapped.status == 504 and "timed out" in str(mapped):
                        run_failed = True
                        self._append_log(
                            {
                                "node_id": self.node_id,
                                "round": round_id,
                                "error": str(mapped),
                                "status": mapped.status,
                                "quorum_timeout": True,
                            }
                        )
                        LOG.warning("%s: %s", self.node_id, mapped)
                        break
                    if mapped.status == 503 and "run failed" in str(mapped):
                        run_failed = True
                        self._append_log(
                            {
                                "node_id": self.node_id,
                                "round": round_id,
                                "error": str(mapped),
                                "status": mapped.status,
                            }
                        )
                        LOG.warning("%s: %s", self.node_id, mapped)
                        break
                    self._append_log(
                        {
                            "node_id": self.node_id,
                            "round": round_id,
                            "error": str(mapped),
                            "status": mapped.status,
                        }
                    )
                    raise mapped
            else:
                blob = pack_message(payload_meta, upload_state)
                query = urllib.parse.urlencode({"node_id": self.node_id, "round": round_id})
                raw = b""
                submit_attempts = 0
                max_submit_attempts = 6
                submitted_ok = False
                while submit_attempts < max_submit_attempts and not submitted_ok:
                    submit_attempts += 1
                    try:
                        _, raw, _, _ = self._request(
                            "POST",
                            f"/v1/update?{query}",
                            blob,
                            content_type="application/octet-stream",
                        )
                        submitted_ok = True
                        break
                    except CoordinatorError as exc:
                        caught = exc
                        msg = str(exc)
                        # Terminal coordinator/run conditions - never retry as transient
                        terminal_timeout = exc.status == 504 and ("timed out" in msg or "quorum" in msg)
                        terminal_run_failed = exc.status == 503 and "run failed" in msg
                        # Transient transport/proxy failures: retry (idempotent recovery handled below)
                        if exc.status in (0, 502) or (exc.status in (503, 504) and not terminal_timeout and not terminal_run_failed):
                            if submit_attempts < max_submit_attempts:
                                LOG.warning(
                                    "%s transient submit error round=%s (attempt %s/%s): %s",
                                    self.node_id, round_id, submit_attempts, max_submit_attempts, exc,
                                )
                                time.sleep(min(2.0 * submit_attempts, 10.0))
                                continue
                        if exc.status == 409 and "duplicate update" in msg:
                            LOG.warning("%s duplicate update detected for round %s; waiting for round result", self.node_id, round_id)
                            try:
                                new_meta, new_state = self._wait_for_round_result(round_id)
                            except CoordinatorError as wait_exc:
                                if submit_attempts < max_submit_attempts:
                                    LOG.warning("%s could not recover duplicate update yet: %s", self.node_id, wait_exc)
                                    time.sleep(min(2.0 * submit_attempts, 10.0))
                                    continue
                                raise wait_exc
                            # Synthesize success path
                            raw = pack_message(new_meta, new_state)
                            submitted_ok = True
                            break
                        if exc.status == 409 and "stale round" in str(exc) and stale_retries < 3:
                            stale_retries += 1
                            self._append_log(
                                {
                                    "node_id": self.node_id,
                                    "round": round_id,
                                    "stale_round_retry": stale_retries,
                                    "error": str(exc),
                                }
                            )
                            LOG.warning(
                                "%s round %s became stale; reloading global state (retry %s/3)",
                                self.node_id,
                                round_id,
                                stale_retries,
                            )
                            new_meta, new_state = self.get_state()
                            if int(new_meta["round"]) <= round_id:
                                raise
                            self._residual = None
                            meta, state = new_meta, clone_state(new_state)
                            submitted_ok = "stale-continue"  # type: ignore[assignment]
                            break
                        if exc.status == 403 and stale_retries < 3:
                            stale_retries += 1
                            try:
                                self.register()
                            except CoordinatorError:
                                pass
                            try:
                                new_meta, new_state = self.get_state()
                            except CoordinatorError:
                                raise caught
                            self._residual = None
                            meta, state = new_meta, clone_state(new_state)
                            submitted_ok = "stale-continue"  # type: ignore[assignment]
                            break
                        if exc.status == 504 and "timed out" in str(exc):
                            run_failed = True
                            self._append_log(
                                {
                                    "node_id": self.node_id,
                                    "round": round_id,
                                    "error": str(exc),
                                    "status": exc.status,
                                    "quorum_timeout": True,
                                }
                            )
                            LOG.warning("%s: %s", self.node_id, exc)
                            break
                        if exc.status == 503 and "run failed" in str(exc):
                            run_failed = True
                            self._append_log(
                                {
                                    "node_id": self.node_id,
                                    "round": round_id,
                                    "error": str(exc),
                                    "status": exc.status,
                                }
                            )
                            LOG.warning("%s: %s", self.node_id, exc)
                            break
                        # 403 "node is not registered" can happen after coordinator restart; retry with re-register
                        if exc.status == 403 and "not registered" in str(exc) and submit_attempts < max_submit_attempts:
                            LOG.warning("%s lost registration, re-registering and retrying submit", self.node_id)
                            try:
                                self.register()
                            except Exception:  # noqa: BLE001
                                pass
                            time.sleep(min(1.5 * submit_attempts, 6.0))
                            continue
                        self._append_log(
                            {
                                "node_id": self.node_id,
                                "round": round_id,
                                "error": str(caught),
                                "status": caught.status,
                            }
                        )
                        raise caught
                if run_failed:
                    break
                if submitted_ok == "stale-continue":
                    continue
                if not submitted_ok:
                    raise CoordinatorError(0, f"submit failed after {max_submit_attempts} attempts")
            new_meta, new_state = unpack_message(raw)
            self._validate_state(new_meta, new_state)
            rounds_done += 1
            stale_retries = 0
            last_metrics = dict(new_meta)
            record = {
                "node_id": self.node_id,
                "round": round_id,
                "new_round": int(new_meta["round"]),
                "global_step": int(new_meta["global_step"]),
                "train_loss": float(stats.get("loss", 0.0)),
                "eval_before": eval_before,
                "eval_after": eval_after,
                "weight": weight,
                "delta_norm": delta_norm,
                "compression": compression,
                "delta_bytes": int(
                    sum(v.numel() * v.element_size() for v in delta.values())
                ),
                "upload_bytes": len(blob),
                "train_seconds": train_seconds,
                "state_hash": str(new_meta.get("state_hash", "")),
                "done": bool(new_meta.get("done", False)),
            }
            self._append_log(record)
            LOG.info(
                "%s round %s -> %s loss=%.6f eval=%s delta_norm=%.6f",
                self.node_id,
                round_id,
                new_meta["round"],
                record["train_loss"],
                "?" if eval_after is None else f"{eval_after:.6f}",
                delta_norm,
            )
            meta, state = new_meta, clone_state(new_state)
            if bool(meta.get("done", False)):
                break

        if not run_failed:
            self.backend.load_state(clone_state(state))
            final_eval = self.backend.evaluate().get("eval_loss")
        else:
            final_eval = None
        summary = {
            "node_id": self.node_id,
            "rounds_done": rounds_done,
            "start_round": start_round,
            "final_round": int(meta["round"]),
            "initial_eval_loss": initial_eval.get("eval_loss"),
            "final_eval_loss": final_eval,
            "registered_at_start": info.get("registered", []),
            "last_metrics": last_metrics,
            "failure": "round timed out waiting for quorum" if run_failed else None,
        }
        self._append_log({"node_id": self.node_id, "summary": summary})
        return summary

    def leave(self) -> None:
        client = self._get_grpc_client()
        if client is not None:
            try:
                client.leave(self.node_id)
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            body = json.dumps({"node_id": self.node_id}).encode("utf-8")
            self._request("POST", "/v1/leave", body)
        except CoordinatorError:
            pass

    def close(self) -> None:
        self._stop_heartbeat()
        self.leave()
        self.backend.close()
