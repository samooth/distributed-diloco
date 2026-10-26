# Estado actual

Repo: `/ai/repos/2026/distributed-diloco`
Commit: `10207e3 feat: initial import of distributed-diloco`
Tests: `22 OK`

## Implementado

- P0/P1: config, backend toy/HF, coordinador HTTP, checkpoints, reanudación.
- Baseline centralizado (`scripts/train_centralized.py`).
- P2.1: compresión int8 por tensor (`train.delta_compression: "int8"`).
- Late join opcional y reintento de ronda obsoleta.
- Carga thread-safe del modelo HF para evitar meta-tensors con 2 workers.

## Resultados

- F1 toy: DiLoCo `0.003723` vs baseline `0.008095`, `loss_gap=-54%`.
- F1 real float (H=10, 2 workers, Qwen2.5-0.5B):
  - inicial: `2.500206`
  - DiLoCo: `2.417826`
  - baseline: `2.297088`
  - `loss_gap=+5.26%`
- F1 real int8 (H=10, 2 workers, Qwen2.5-0.5B, 5 rondas):
  - inicial: `2.491262`
  - DiLoCo: `2.418675`
  - baseline: `2.297088`
  - `loss_gap=+5.29%`
  - upload: `567341-567342 B` vs delta `2162688 B` (3.8x)
- F1 real int8 (H=20, 2 workers, Qwen2.5-0.5B, 5 rondas):
  - inicial: `2.491262`
  - DiLoCo: `2.325399`
  - baseline: `2.297088`
  - `loss_gap=+1.23%`
  - upload: `567341-567343 B` vs delta `2162688 B` (3.8x)
- F1 real int8 (H=50, 2 workers, Qwen2.5-0.5B, 5 rondas):
  - inicial: `2.491262`
  - DiLoCo: `2.274715`
  - baseline: `2.297088`
  - `loss_gap=-0.97%`
  - upload: `567341-567343 B` vs delta `2162688 B` (3.8x)
- F1 real int8 (H=100, 2 workers, Qwen2.5-0.5B, 5 rondas):
  - inicial: `2.491262`
  - DiLoCo: `2.263300`
  - baseline: `2.297088`
  - `loss_gap=-1.47%`
  - upload: `567341-567343 B` vs delta `2162688 B` (3.8x)
- F1 real int8+top-k (H=10, 2 workers, Qwen2.5-0.5B, 5 rondas):
  - inicial: `2.491262`
  - DiLoCo: `2.474486`
  - baseline: `2.297088`
  - `loss_gap=+7.72%`
  - upload: `544057-544059 B` vs delta `2162688 B` (4.0x)
- int8 toy: `0.003339` vs `0.003723`, sin degradación apreciable.
- int8 HF de 1 paso: payload `2.16 MB -> 0.54 MB` (4x), mensaje HTTP `0.567 MB` (3.8x).

## Implementado adicionalmente

- Transporte P2P opcional con `--p2p-topic`: el coordinador publica un túnel
  Hyperswarm y los workers se conectan a un puerto local.

- Sidecar opcional Holepunch en `network/holepunch-sidecar/`.
- Emulación WAN con `scripts/wan_emulate.py`.
- Object store local/S3 integrado en checkpoints.
- Tests de object store, evicción, leave y retención.

## Pendiente

- Ejecutar F1 int8 de varias rondas con 2 workers y documentar `loss_gap`.
- Top-k y error feedback.
- gRPC/mTLS, expulsión automática de stragglers y churn.
- Pruebas WAN con `tc netem`.

## Comandos útiles

```bash
../hf-venv/bin/python -m unittest discover -s tests -q
../hf-venv/bin/python scripts/smoke_local.py --config configs/smoke.json --reset --rounds 4 --workers 2 --device cpu
../hf-venv/bin/python scripts/smoke_local.py --config configs/f1-qwen-0.5b.json --reset --rounds 5 --workers 2 --device cpu
../hf-venv/bin/python scripts/train_centralized.py --config configs/f1-qwen-0.5b.json --device cpu
```

## Entorno

- GPU local ocupada por PID `90605`; no matar.
- Usar `hf-venv/bin/python`.
- Modelos locales en `/ai/models/Qwen2.5-0.5B-Instruct`.
