#!/usr/bin/env python3
"""Small TCP proxy for WAN emulation.

It is dependency-free and can sit between a worker and the coordinator. It adds
one-way delay, jitter and random packet loss to every forwarded chunk. It is a
coarse emulator, but it is enough to exercise timeouts, retries and stragglers
without changing the DiLoCo code.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


async def _pipe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    delay_ms: float,
    jitter_ms: float,
    loss: float,
    label: str,
) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            if loss > 0 and random.random() < loss:
                logging.debug("%s dropped %s bytes", label, len(chunk))
                continue
            delay = max(0.0, delay_ms + random.uniform(-jitter_ms, jitter_ms)) / 1000.0
            if delay:
                await asyncio.sleep(delay)
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def _handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
    delay_ms: float,
    jitter_ms: float,
    loss: float,
) -> None:
    try:
        server_reader, server_writer = await asyncio.open_connection(target_host, target_port)
    except OSError as exc:
        logging.warning("cannot connect to %s:%s: %s", target_host, target_port, exc)
        client_writer.close()
        await client_writer.wait_closed()
        return

    client_to_server = asyncio.create_task(
        _pipe(client_reader, server_writer, delay_ms, jitter_ms, loss, "client->server")
    )
    server_to_client = asyncio.create_task(
        _pipe(server_reader, client_writer, delay_ms, jitter_ms, loss, "server->client")
    )
    await asyncio.gather(client_to_server, server_to_client, return_exceptions=True)


async def main() -> None:
    parser = argparse.ArgumentParser(description="WAN emulation proxy")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, required=True)
    parser.add_argument("--delay-ms", type=float, default=0.0)
    parser.add_argument("--jitter-ms", type=float, default=0.0)
    parser.add_argument("--loss", type=float, default=0.0, help="packet/chunk loss probability 0..1")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not 0.0 <= args.loss <= 1.0:
        raise SystemExit("--loss must be between 0 and 1")

    server = await asyncio.start_server(
        lambda r, w: _handle_client(
            r,
            w,
            args.target_host,
            args.target_port,
            args.delay_ms,
            args.jitter_ms,
            args.loss,
        ),
        args.listen_host,
        args.listen_port,
    )
    logging.info(
        "proxy %s:%s -> %s:%s delay=%sms jitter=%sms loss=%.3f",
        args.listen_host,
        args.listen_port,
        args.target_host,
        args.target_port,
        args.delay_ms,
        args.jitter_ms,
        args.loss,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
