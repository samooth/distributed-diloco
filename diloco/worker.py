"""DiLoCo worker: local adapter steps + pseudo-gradient upload + global reload."""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import torch

from .backend import AdapterBackend
from .compression import encode_int8
from .config import ExperimentConfig
from .protocol import pack_message, unpack_message
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
    ) -> None:
        self.config = config
        self.node_id = str(node_id)
        self.coordinator_url = coordinator_url.rstrip("/")
        self.backend = backend
        self.log_path = Path(log_path) if log_path else None
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._timeout = float(config.coordinator.round_timeout_s) + 60.0
        idle = float(config.coordinator.idle_timeout_s)
        self._heartbeat_interval = 30.0 if idle <= 0 else max(1.0, min(30.0, idle / 3.0))
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None

    def _headers(self) -> Dict[str, str]:
        token = self.config.coordinator.token
        return {"X-DiLoCo-Token": token} if token else {}

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        content_type: str = "application/json",
        timeout: Optional[float] = None,
    ) -> tuple[int, bytes, str]:
        url = self.coordinator_url + path
        headers = self._headers()
        if body is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                request,
                timeout=self._timeout if timeout is None else timeout,
            ) as response:
                return int(response.status), response.read(), response.headers.get_content_type()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            message = raw.decode("utf-8", errors="replace")
            try:
                payload = json.loads(message)
                message = str(payload.get("error", message))
            except json.JSONDecodeError:
                pass
            raise CoordinatorError(exc.code, message) from exc
        except urllib.error.URLError as exc:
            raise CoordinatorError(0, f"cannot reach coordinator: {exc.reason}") from exc

    def register(self) -> Dict[str, Any]:
        body = json.dumps(
            {
                "node_id": self.node_id,
                "config_hash": self.config.fingerprint(),
                "base_hash": self.backend.base_fingerprint(),
            }
        ).encode("utf-8")
        status, raw, _ = self._request("POST", "/v1/register", body)
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
            try:
                self._request("GET", f"/v1/heartbeat?{query}", timeout=10.0)
            except CoordinatorError as exc:
                if exc.status in (403, 410):
                    return
                LOG.debug("%s heartbeat failed: %s", self.node_id, exc)
            except Exception as exc:  # noqa: BLE001
                LOG.debug("%s heartbeat failed: %s", self.node_id, exc)

    def get_state(self) -> tuple[Dict[str, Any], Dict[str, torch.Tensor]]:
        query = urllib.parse.urlencode({"node_id": self.node_id})
        _, raw, _ = self._request("GET", f"/v1/state?{query}")
        meta, state = unpack_message(raw)
        self._validate_state(meta, state)
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
        info = self.register()
        self._start_heartbeat()
        try:
            return self._run_registered(info)
        finally:
            self._stop_heartbeat()

    def _run_registered(self, info: Mapping[str, Any]) -> Dict[str, Any]:
        meta, state = self.get_state()
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
            if compression == "int8":
                upload_state = encode_int8(delta)
            elif compression == "none":
                upload_state = delta
            else:
                raise RuntimeError(f"unsupported delta compression: {compression}")
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
            blob = pack_message(payload_meta, upload_state)
            query = urllib.parse.urlencode({"node_id": self.node_id, "round": round_id})
            try:
                _, raw, _ = self._request(
                    "POST",
                    f"/v1/update?{query}",
                    blob,
                    content_type="application/octet-stream",
                )
            except CoordinatorError as exc:
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
                    meta, state = new_meta, clone_state(new_state)
                    continue
                self._append_log(
                    {
                        "node_id": self.node_id,
                        "round": round_id,
                        "error": str(exc),
                        "status": exc.status,
                    }
                )
                raise
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

        self.backend.load_state(clone_state(state))
        final_eval = self.backend.evaluate().get("eval_loss")
        summary = {
            "node_id": self.node_id,
            "rounds_done": rounds_done,
            "start_round": start_round,
            "final_round": int(meta["round"]),
            "initial_eval_loss": initial_eval.get("eval_loss"),
            "final_eval_loss": final_eval,
            "registered_at_start": info.get("registered", []),
            "last_metrics": last_metrics,
        }
        self._append_log({"node_id": self.node_id, "summary": summary})
        return summary

    def close(self) -> None:
        self._stop_heartbeat()
        self.backend.close()
