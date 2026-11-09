# ◆ DiLoCo WAN

> **Train LoRA adapters for large models over the internet, without a datacenter.**

DiLoCo WAN uses **DiLoCo** (*Distributed Low-Communication*): it synchronizes only the LoRA adapter every `H` local steps with an outer Nesterov optimizer. The base model stays frozen (NF4/QLoRA), so each node fits on consumer GPUs (8–80 GB).

## Features
- **Backends**: `toy` (deterministic CPU) + `hf` (PEFT/QLoRA), thread-safe loading.
- **Coordinator**: HTTP/gRPC, token, quorum, deadline, heartbeat, late join, `/v1/state_meta`.
- **Compression**: int8 (4×), top-k + error feedback.
- **Churn**: rejoin, eviction, recovery in ≤ 2 rounds (tested).
- **P2P/WAN**: **bitbeam v4** (DHT) persistent multiplexed tunnel (`--p2p-topic`), WAN emulation.
- **HF bootstrap**: auto-discover `runs/init-<config-stem>`, `--init-state auto` (coordinator export), local init-state bootstrap when hashes match.
- **UX**: root wrappers `./coordinator` `./worker` `./agent`, wizards, web console/monitor.

## Quick start
```bash
.venv/bin/python -m unittest discover -s tests -q
./coordinator --config configs/smoke.json --device cpu --reset --max-rounds 4 --no-wait
./worker --config configs/smoke.json --node-id node-0 --coordinator http://127.0.0.1:8095 --device cpu
./agent --open  # serves web/ and opens dashboard
```
P2P tunnel: `cd network/holepunch-sidecar && npm install`, then add `--p2p-topic <topic>` to coordinator/workers. Repo configs use path placeholders.

## Documentation
- [Onboarding](../ONBOARDING.md) · [Communication](../03-comunicacion.md) · [Security](../07-seguridad.md)
- [Changelog](../../CHANGELOG.md) · [Status](../../STATUS.md) · [Roadmap](../../ROADMAP.md)
