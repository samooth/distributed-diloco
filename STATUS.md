# Estado actual

Repo: `/ai/repos/2026/distributed-diloco`
Commit: `3f87241 feat: add Hyperswarm P2P transport tunnel`
Tests: `34 OK`

## Implementado

- P0/P1: config, backend toy/HF, coordinador HTTP, checkpoints, reanudación.
- Baseline centralizado (`scripts/train_centralized.py`).
- P2.1: compresión int8 por tensor (`train.delta_compression: "int8"`).
- P2.2: top-k, error feedback, late join, salida ordenada, expulsión básica,
  retención de checkpoints, TLS opcional y object store local/S3.
- Transporte P2P opcional con `--p2p-topic` (túnel Hyperswarm).
- Sidecar Holepunch para discovery, ledger, Hyperdrive y HyperDB.
- Emulación WAN con `scripts/wan_emulate.py`.
- Carga thread-safe del modelo HF para evitar meta-tensors con 2 workers.
- `run_coordinator.py --reset`.

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
- gRPC/mTLS real y churn completo.
- Test de signo del pseudo-gradiente.
- Pilotos 32B/70B en nodos con GPU suficiente.

## Comandos útiles

```bash
../hf-venv/bin/python -m unittest discover -s tests -q
../hf-venv/bin/python scripts/smoke_local.py --config configs/smoke.json --reset --rounds 4 --workers 2 --device cpu
../hf-venv/bin/python scripts/run_coordinator.py --config configs/smoke.json --device cpu --reset --max-rounds 4 --p2p-topic diloco-exp --no-wait
../hf-venv/bin/python scripts/run_worker.py --config configs/smoke.json --node-id node-0 --p2p-topic diloco-exp --shard-id 0 --device cuda --tunnel-listen-port 18183
```

## Entorno

- GPU disponible en esta máquina: RTX 3080 8 GB.
- Usar `hf-venv/bin/python`.
- Modelos locales en `/ai/models/Qwen2.5-0.5B-Instruct`.
