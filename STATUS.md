# Estado actual

Repo: `/ai/repos/2026/distributed-diloco`
Commit: `f4b5bc3 feat: add real gRPC/mTLS transport`
Tests: `36 OK` (entorno `.venv`)

## Implementado

- P0/P1: config, backend toy/HF, coordinador HTTP, espera de `min_workers`,
  checkpoints, reanudación.
- Baseline centralizado (`scripts/train_centralized.py`).
- P2.1: compresión int8 por tensor (`train.delta_compression: "int8"`).
- P2.2: top-k, error feedback, late join, salida ordenada, expulsión básica,
  retención de checkpoints, TLS opcional y object store local/S3.
- Transporte P2P opcional con `--p2p-topic` (túnel Hyperswarm).
- Sidecar Holepunch para discovery, ledger, Hyperdrive y HyperDB.
- Emulación WAN con `scripts/wan_emulate.py`.
- Carga thread-safe del modelo HF para evitar meta-tensors con 2 workers.
- `run_coordinator.py --reset`.
- gRPC/mTLS real (transporte alternativo al HTTP/1.0).
- Página web de presentación en `web/index.html`.
- Gateway DiLoCo (Hyperswarm/WebSocket) integrado en
  `network/diloco-gateway/`.
- Adaptador Python `diloco/gateway_client.py` (`DiLoCoGatewayClient`) para
  relay de deltas/checkpoints vía WebSocket.

## Resultados

- F1 toy: DiLoCo `0.003723` vs baseline `0.008095`, `loss_gap=-54%`.
- F1 real float (H=10, 2 workers, Qwen2.5-0.5B):
  - inicial: `2.500206`
  - DiLoCo: `2.417826`
  - baseline: `2.297088`
  - `loss_gap=+5.26%`
- F1 real int8 (2 workers, Qwen2.5-0.5B, 5 rondas):
  - H=10: `2.418675`, `loss_gap=+5.29%`, upload `567341-567342 B`
  - H=20: `2.325399`, `loss_gap=+1.23%`
  - H=50: `2.274715`, `loss_gap=-0.97%`
  - H=100: `2.263300`, `loss_gap=-1.47%`
  - delta por ronda: `2162688 B` (3.8x)
- F1 real int8+top-k 10% (2 workers, 5 rondas, error feedback):
  - H=10: `2.474486`, `loss_gap=+7.72%`, upload `544057-544059 B`
  - H=20: `2.445505`, `loss_gap=+6.46%`
  - H=50: `2.399221`, `loss_gap=+4.45%`
  - H=100: `2.369196`, `loss_gap=+3.14%`
- F1 real int8 con error feedback (2 workers, 5 rondas):
  - H=10: `2.467926`, `loss_gap=+7.44%`
  - H=20: `2.435299`, `loss_gap=+6.02%`
  - H=50: `2.380166`, `loss_gap=+3.62%`
- int8 toy: `0.003339` vs `0.003723`, sin degradación apreciable.
- int8 HF de 1 paso: payload `2.16 MB -> 0.54 MB` (4x), mensaje HTTP `0.567 MB` (3.8x).

## Pendiente

- Barrido int8 con error feedback a `H=100`.
- Pruebas WAN con `tc netem` y medición de CU/sync.
- Churn: rejoin de nodos conocidos, recuperación 403 y test integrado de caída a
  mitad de ronda con recuperación en <= 2 rondas.
- Test de signo del pseudo-gradiente (unitario añadido; falta validación
  integrada de divergencia con signo invertido).
- Pilotos 32B/70B en nodos con GPU suficiente.
- Revisión de OpenDiLoCo: útil como referencia, no base (ver docs/09).
## Comandos útiles

```bash
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python scripts/smoke_local.py --config configs/smoke.json --reset --rounds 4 --workers 2 --device cpu
.venv/bin/python scripts/run_coordinator.py --config configs/smoke.json --device cpu --reset --max-rounds 4 --p2p-topic diloco-exp --no-wait
.venv/bin/python scripts/run_worker.py --config configs/smoke.json --node-id node-0 --p2p-topic diloco-exp --shard-id 0 --device cuda --tunnel-listen-port 18183
```

## Entorno

- GPU disponible en esta máquina: RTX 3080 8 GB.
- Usar `hf-venv/bin/python`.
- Modelos locales en `/ai/models/Qwen2.5-0.5B-Instruct`.
