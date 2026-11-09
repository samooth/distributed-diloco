# Changelog

Notas detalladas de cambios y decisiones. El `README.md` es la presentación.

## [Unreleased]

### Añadido
- **Bitbeam v4 como transporte de túnel P2P** (`network/holepunch-sidecar`): `tunnel-server`/`tunnel-client` usan `bitbeam@4` sobre `@hyperswarm/dht` con **multiplexado persistente** de substreams TCP (una conexión P2P por worker, no un lookup por request HTTP). Clave determinista derivada de `--p2p-topic`.
- **`GET /v1/state_meta`** (coordinador, autenticado): metadatos de ronda/estado sin descargar el blob completo.
- **Bootstrap local de estado inicial** (worker): `--init-state` carga el bundle local si `state_hash`/`base_hash`/`config_hash` coinciden con el coordinador; evita descargas grandes por túnel en round 0.
- **Autodetección/export de init state**: `run_worker.py`/`run_coordinator.py` descubren `runs/init-<config-stem>` para HF; `--init-state auto` en coordinador exporta el adaptador si falta.
- **Wrappers raíz**: `./coordinator`, `./worker`, `./agent` (usan `.venv/bin/python` si existe).
- **Agent web**: `scripts/agent.py` sirve `web/` en el mismo origen, añade `--open` y pasa `init_state` al lanzar workers.
- **Reintentos de red** en worker para requests seguras (`GET`, heartbeat/register/leave) ante `IncompleteRead`/desconexiones.

### Cambiado
- `/v1/status` (y alias `/v1/metrics`) incluye `config_hash`.
- `configs/qwen3-4b-2nodes.json`: `num_shards=4` (cumple `>= max_workers`) y rutas/modelo como placeholders.
- Documentación actualizada (README, ONBOARDING, 03-comunicación, STATUS, ROADMAP).

### Notas
- El túnel bitbeam requiere `cd network/holepunch-sidecar && npm install` en cada máquina. Tras cambios en `index.js`, reiniciar procesos que lanzan el sidecar.
- `--init-state auto` en coordinador puede cargar el modelo base; para 32B/70B exportar en una máquina adecuada y distribuir el bundle.
- `scripts/agent.py` es para entornos confiables (sin auth fuerte por defecto).

## [Previo] — base P0–P2

### Implementado
- P0/P1: config validada con fingerprint, backend `toy`/`hf` (PEFT/QLoRA), coordinador HTTP/gRPC, checkpoints, reanudación, exportación inicial.
- P2.1: compresión int8 por tensor, top-k, error feedback.
- P2.2: rejoin, late join, evicción, TLS/mTLS, object store, WAN emulation, transporte P2P (inicial Hyperswarm), churn completo (≤ 2 rondas).
- Carga thread-safe del modelo HF. Wizards, gateway DiLoCo, consola web, monitor de red.

### Resultados
- F1 toy: DiLoCo `0.003723` vs baseline `0.008095`, `loss_gap=-54%`.
- Qwen2.5-0.5B (2 workers, 5 rondas): int8 H=10/20/50/100 → `+5.29%`, `+1.23%`, `-0.97%`, `-1.47%`; int8+top-k 10% → `+7.72%`, `+6.46%`, `+4.45%`, `+3.14%`.
- Payload Qwen2.5-0.5B: 2.16 MB → 0.54 MB (4×) con int8.

### Pendiente
- Validación E2E bitbeam en NAT real; identidad BSV por nodo (`@bsv/sdk`); `pilot/up`; barridos WAN con `tc netem`; pilotos 32B/70B.

### Referencias esenciales
- DiLoCo: https://arxiv.org/abs/2311.08105 · OpenDiLoCo: https://arxiv.org/abs/2407.07852 · INTELLECT-1: https://arxiv.org/abs/2412.01152 · Streaming DiLoCo: https://arxiv.org/abs/2501.18512 · SparseLoCo: https://arxiv.org/abs/2508.15706 · DiLoCoX: https://arxiv.org/abs/2506.21263. Lista completa: `docs/09-referencias.md`.
