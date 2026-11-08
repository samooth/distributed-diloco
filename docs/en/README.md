# ◆ DiLoCo WAN

> **Train 32B/70B LoRA adapters over the internet, without a datacenter.**

DiLoCo WAN adapts large models using **DiLoCo** (*Distributed Low-Communication*):
it synchronizes only the adapter every `H` local steps, with an outer Nesterov
optimizer. The base stays frozen in NF4 (QLoRA), so each island fits on a
consumer GPU (8–80 GB) and traffic per round drops from **140 GB to 0.07–0.8 GB**.

## Features

- **Backends**: `toy` (deterministic CPU) + `hf` (PEFT/QLoRA), thread-safe loading.
- **Coordinator**: HTTP/gRPC, token, quorum, deadline, heartbeat, late join.
- **Compression**: int8 (4×), top-k + error feedback.
- **Churn**: rejoin, eviction, recovery in ≤ 2 rounds (integrated test).
- **Security**: TLS/mTLS, allowlist, delta validation.
- **Storage**: atomic checkpoints, retention, local/S3 object store.
- **WAN**: emulation (delay, jitter, loss, bandwidth), Hyperswarm tunnel.
- **Web**: landing + operational console + network monitor (responsive).
- **CLI**: classic wizard and TUI (curses).
- **Agent**: process launcher from the web UI.

## Quick start

```bash
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python scripts/smoke_local.py --reset --rounds 4 --workers 2 --device cpu
.venv/bin/python scripts/wizard.py
```

## Documentation

- [Onboarding](../ONBOARDING.md) — from zero to your first round.
- [Algorithm](../01-algoritmo.md) · [Architecture](../02-arquitectura.md) · [Communication](../03-comunicacion.md)
- [Models](../04-modelos.md) · [Training](../05-entrenamiento.md) · [Operations](../06-operaciones.md)
- [Security](../07-seguridad.md) · [Experiments](../08-experimentos.md) · [References](../09-referencias.md)
- [Changelog](../../CHANGELOG.md) · [Status](../../STATUS.md) · [Roadmap](../../ROADMAP.md)