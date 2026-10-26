"""Small object-store abstraction for checkpoints and artifacts.

The local implementation is always available. The S3 implementation is optional
and is only imported when boto3 is installed, so the rest of the package does
not gain a hard dependency on cloud SDKs.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class ObjectStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...

    def list(self, prefix: str = "") -> List[str]: ...


class LocalObjectStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        key = key.strip("/")
        if not key or ".." in Path(key).parts:
            raise ValueError(f"invalid object key: {key!r}")
        return self.root / key

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise FileNotFoundError(f"object not found: {key}")
        return path.read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

    def list(self, prefix: str = "") -> List[str]:
        base = self._path(prefix) if prefix else self.root
        if not base.exists():
            return []
        if base.is_file():
            return [prefix.strip("/")]
        result: List[str] = []
        for path in base.rglob("*"):
            if path.is_file():
                result.append(path.relative_to(self.root).as_posix())
        return sorted(result)


class S3ObjectStore:
    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        region: Optional[str] = None,
        endpoint_url: Optional[str] = None,
    ) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("boto3 is required for S3ObjectStore") from exc
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url)

    def _key(self, key: str) -> str:
        key = key.strip("/")
        if self.prefix:
            return f"{self.prefix}/{key}"
        return key

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def get(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        return response["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))

    def list(self, prefix: str = "") -> List[str]:
        full_prefix = self._key(prefix)
        if full_prefix and not full_prefix.endswith("/"):
            full_prefix += "/"
        paginator = self.client.get_paginator("list_objects_v2")
        result: List[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for item in page.get("Contents", []):
                key = item["Key"]
                if self.prefix and key.startswith(self.prefix + "/"):
                    key = key[len(self.prefix) + 1 :]
                result.append(key)
        return sorted(result)


def sync_directory(store: ObjectStore, directory: str, prefix: str = "") -> List[str]:
    root = Path(directory)
    uploaded: List[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        key = f"{prefix.strip('/')}/{relative}".strip("/") if prefix else relative
        store.put(key, path.read_bytes())
        uploaded.append(key)
    return sorted(uploaded)


def create_object_store(uri: str, prefix: str = "") -> Optional[ObjectStore]:
    uri = uri.strip()
    if not uri:
        return None
    if uri.startswith("s3://"):
        rest = uri[len("s3://"):]
        bucket, _, base_prefix = rest.partition("/")
        full_prefix = "/".join(part for part in (base_prefix, prefix) if part)
        return S3ObjectStore(bucket=bucket, prefix=full_prefix)
    root = uri
    if prefix:
        root = str(Path(root) / prefix)
    return LocalObjectStore(root)
