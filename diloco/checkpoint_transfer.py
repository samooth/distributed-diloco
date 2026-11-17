"""Chunked, resumable and verified checkpoint artifact download (HTTP).

Complements the state-blob bootstrap path: checkpoint files (``adapter.safetensors``,
``outer.safetensors``, ``meta.json``) can be large for 32B/70B LoRA. The client
uses the coordinator manifest (per-chunk SHA-256 + Merkle root + file SHA-256),
resumes from a partial file, retries transient failures with backoff, reuses a
persistent cache keyed by the checkpoint round/state hash, and transparently
decompresses negotiated wire-compressed ranges.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .wire import available_wire_codecs, decompress_wire, normalize_codec, parse_accept_wire

CHECKPOINT_FILES = ("adapter.safetensors", "outer.safetensors", "meta.json")


class CheckpointTransferError(Exception):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _merkle_root(chunk_hashes: list[str]) -> str:
    if not chunk_hashes:
        return _sha256(b"")
    level = [bytes.fromhex(h) for h in chunk_hashes]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [hashlib.sha256(level[i] + level[i + 1]).digest() for i in range(0, len(level), 2)]
    return level[0].hex()


def verify_artifact(blob: bytes, manifest: Mapping[str, Any]) -> bool:
    expected_sha = str(manifest.get("sha256", "") or "")
    chunk_hashes = [str(h) for h in list(manifest.get("chunk_hashes") or [])]
    chunk_size = int(manifest.get("chunk_size", 0) or 0)
    if expected_sha and _sha256(blob) != expected_sha:
        return False
    if chunk_hashes and chunk_size > 0:
        got = [_sha256(blob[i : i + chunk_size]) for i in range(0, len(blob), chunk_size)]
        if got != chunk_hashes:
            return False
        if _merkle_root(chunk_hashes) != str(manifest.get("merkle_root", "")):
            return False
    return True


class CheckpointDownloader:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        cache_dir: Optional[str] = None,
        timeout: float = 60.0,
        max_attempts: int = 8,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.token = str(token or "")
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir else None
        self.timeout = float(timeout)
        self.max_attempts = int(max_attempts)
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.last_wire_bytes = 0

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self.token:
            headers["X-DiLoCo-Token"] = self.token
        accept = ",".join(available_wire_codecs())
        if accept:
            headers["X-DiLoCo-Accept-Wire-Compression"] = accept
        if extra:
            headers.update(extra)
        return headers

    def _request(self, path: str, headers: Optional[Dict[str, str]] = None) -> tuple[int, bytes, Dict[str, str]]:
        url = self.base_url + path
        req = urllib.request.Request(url, headers=self._headers(headers), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                status = int(response.status)
                raw = response.read()
                resp_headers = {k.lower(): v for k, v in response.headers.items()}
                codec = normalize_codec(resp_headers.get("x-diloco-wire-compression", ""))
                if codec and raw:
                    try:
                        raw = decompress_wire(raw, codec)
                    except Exception as exc:  # noqa: BLE001
                        raise CheckpointTransferError(f"wire decompress failed: {exc}") from exc
                self.last_wire_bytes = int(resp_headers.get("_wire_bytes", len(raw)) or len(raw))
                return status, raw, resp_headers
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            codec = normalize_codec(exc.headers.get("x-diloco-wire-compression", "")) if exc.headers else ""
            if codec and raw:
                try:
                    raw = decompress_wire(raw, codec)
                except Exception:
                    pass
            message = raw.decode("utf-8", errors="replace")
            try:
                message = str(json.loads(message).get("error", message))
            except json.JSONDecodeError:
                pass
            raise CheckpointTransferError(f"HTTP {exc.code}: {message}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CheckpointTransferError(f"connection error: {exc}") from exc

    def fetch_meta(self, round_id: int = 0) -> Dict[str, Any]:
        query = urllib.parse.urlencode({"round": int(round_id)})
        status, raw, _ = self._request(f"/v1/checkpoint_meta?{query}")
        if status != 200:
            raise CheckpointTransferError(f"unexpected status {status}")
        try:
            meta = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise CheckpointTransferError(f"invalid checkpoint meta: {exc}") from exc
        if not isinstance(meta, dict) or not isinstance(meta.get("files"), list):
            raise CheckpointTransferError("checkpoint meta missing files")
        return meta

    def _cache_path(self, meta: Mapping[str, Any], name: str) -> Optional[Path]:
        if self.cache_dir is None:
            return None
        key = f"{int(meta.get('round', 0)):06d}-{str(meta.get('state_hash', ''))[:16]}-{name}"
        return self.cache_dir / key

    def download_file(self, meta: Mapping[str, Any], name: str) -> bytes:
        spec = next((f for f in meta["files"] if str(f.get("name")) == name), None)
        if spec is None:
            raise CheckpointTransferError(f"checkpoint file not in manifest: {name}")
        expected_size = int(spec.get("size", 0) or 0)
        chunk_size = int(spec.get("chunk_size", 0) or 0) or 4 * 1024 * 1024
        cache = self._cache_path(meta, name)

        buf = bytearray()
        offset = 0
        if cache is not None and cache.is_file():
            partial = cache.read_bytes()
            # Keep only a prefix that still verifies against manifest chunks.
            usable = min(len(partial), expected_size)
            verified = 0
            for idx in range(0, (usable + chunk_size - 1) // chunk_size):
                cstart = idx * chunk_size
                cend = min(expected_size, cstart + chunk_size)
                if usable < cend:
                    break
                chunk_hashes = list(spec.get("chunk_hashes") or [])
                if chunk_hashes and idx < len(chunk_hashes) and _sha256(partial[cstart:cend]) != str(chunk_hashes[idx]):
                    break
                verified = cend
            buf = bytearray(partial[:verified])
            offset = verified

        attempts = 0
        while offset < expected_size and attempts < self.max_attempts:
            end = min(expected_size - 1, offset + chunk_size - 1)
            query = urllib.parse.urlencode({
                "round": int(meta.get("round", 0)),
                "name": name,
                "offset": offset,
                "length": chunk_size,
            })
            try:
                status, data, headers = self._request(
                    f"/v1/checkpoint_file?{query}",
                    {"Range": f"bytes={offset}-{end}"},
                )
            except CheckpointTransferError as exc:
                attempts += 1
                time.sleep(min(1.5 * attempts, 8.0))
                if attempts >= self.max_attempts:
                    raise
                continue
            if status not in (200, 206) or not data:
                attempts += 1
                time.sleep(min(1.0 * attempts, 4.0))
                continue
            chunk_hashes = list(spec.get("chunk_hashes") or [])
            if chunk_hashes:
                idx = offset // chunk_size
                if idx < len(chunk_hashes) and _sha256(data) != str(chunk_hashes[idx]):
                    attempts += 1
                    continue
            buf.extend(data)
            offset += len(data)
            if cache is not None:
                cache.write_bytes(bytes(buf))
            if str(headers.get("x-diloco-checkpoint-sha256", "")) and offset >= expected_size:
                break

        blob = bytes(buf[:expected_size]) if expected_size else bytes(buf)
        if not verify_artifact(blob, spec):
            if cache is not None and cache.exists():
                cache.unlink()
            raise CheckpointTransferError(f"checkpoint artifact verification failed: {name}")
        if cache is not None:
            cache.write_bytes(blob)
        return blob

    def download_all(self, round_id: int = 0, names: Optional[list[str]] = None) -> Dict[str, bytes]:
        meta = self.fetch_meta(round_id)
        selected = list(names) if names is not None else [str(f["name"]) for f in meta["files"]]
        result: Dict[str, bytes] = {}
        for name in selected:
            result[name] = self.download_file(meta, name)
        return result
