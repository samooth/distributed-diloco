"""Wire-level (transport) compression for HTTP/gRPC payloads.

This is separate from ML delta compression (int8/top-k) in compression.py.
Wire compression reduces bytes transferred for bootstrap/state blobs and
update submissions. It is negotiated via headers/metadata and must preserve
backward compatibility with peers that do not support it.
"""
from __future__ import annotations

import gzip
import io
from typing import Iterable, List, Optional, Tuple

from .compression import zstd_available

# Codecs supported by this runtime, in preference order for auto-negotiation.
SUPPORTED_WIRE_CODECS: Tuple[str, ...] = ("zstd", "gzip") if zstd_available() else ("gzip",)


def _zstd_compress(data: bytes) -> bytes:
    try:
        from compression import zstd  # Python 3.14+
        return zstd.compress(data)
    except Exception:
        pass
    try:
        import zstandard as zstd  # type: ignore
        return zstd.ZstdCompressor(level=3).compress(data)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"zstd compression unavailable: {exc}") from exc


def _zstd_decompress(data: bytes) -> bytes:
    try:
        from compression import zstd  # Python 3.14+
        return zstd.decompress(data)
    except Exception:
        pass
    try:
        import zstandard as zstd  # type: ignore
        return zstd.ZstdDecompressor().decompress(data)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"zstd decompression unavailable: {exc}") from exc


def normalize_codec(codec: str) -> str:
    c = str(codec or "").strip().lower()
    aliases = {"x-gzip": "gzip", "none": "", "identity": ""}
    c = aliases.get(c, c)
    return c


def available_wire_codecs() -> List[str]:
    codecs = ["gzip"]
    if zstd_available():
        codecs.insert(0, "zstd")
    return codecs


def codec_available(codec: str) -> bool:
    c = normalize_codec(codec)
    if not c:
        return True
    if c == "gzip":
        return True
    if c == "zstd":
        return zstd_available()
    return False


def compress_wire(data: bytes, codec: str) -> bytes:
    c = normalize_codec(codec)
    if not c:
        return data
    if c == "gzip":
        out = io.BytesIO()
        with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=6, mtime=0) as f:
            f.write(data)
        return out.getvalue()
    if c == "zstd":
        return _zstd_compress(data)
    raise ValueError(f"unsupported wire compression codec: {codec!r}")


def decompress_wire(data: bytes, codec: str) -> bytes:
    c = normalize_codec(codec)
    if not c:
        return data
    if c == "gzip":
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as f:
            return f.read()
    if c == "zstd":
        return _zstd_decompress(data)
    raise ValueError(f"unsupported wire compression codec: {codec!r}")


def parse_accept_wire(header_value: str) -> List[str]:
    # Accepts comma/space separated codecs, optionally with q-values (ignored except q=0)
    out: List[str] = []
    for part in str(header_value or "").split(","):
        token = part.strip()
        if not token:
            continue
        pieces = [p.strip() for p in token.split(";") if p.strip()]
        if not pieces:
            continue
        name = normalize_codec(pieces[0])
        q = 1.0
        for p in pieces[1:]:
            if p.startswith("q="):
                try:
                    q = float(p[2:])
                except Exception:
                    q = 1.0
        if q <= 0.0 or not name:
            continue
        if name not in out:
            out.append(name)
    return out


def negotiate_wire_codec(preferred: str, accepted: Iterable[str]) -> str:
    accepted_set = {normalize_codec(a) for a in accepted if normalize_codec(a)}
    pref = normalize_codec(preferred)
    if pref == "auto":
        for c in available_wire_codecs():
            if c in accepted_set and codec_available(c):
                return c
        return ""
    if not pref:
        return ""
    if pref in accepted_set and codec_available(pref):
        return pref
    # If preferred is configured but client didn't explicitly advertise, allow it only
    # when caller treats preferred as both configured and accepted; negotiation is explicit here.
    return ""
