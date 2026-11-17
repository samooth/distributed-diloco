# Imágenes Docker GPU (CUDA + HF/PEFT + bitbeam)

Permiten entrenar workers en máquinas con NVIDIA sin instalar nada en el host
más que el driver y el NVIDIA Container Toolkit.

## Requisitos del host

- Driver NVIDIA (se probó con 580.x / CUDA 13; el runtime CUDA 12.8 de la imagen
  es compatible por backwards-compatibility).
- Docker Engine + Compose v2 y NVIDIA Container Toolkit:
  ```bash
  docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu22.04 nvidia-smi
  ```

## Construcción

```bash
docker build -f Dockerfile.gpu -t diloco-wan-gpu:latest .
```

La imagen incluye:

- CUDA runtime + cuDNN (base `nvidia/cuda:12.8.0-cudnn-runtime-ubuntu22.04`).
- PyTorch CUDA (`torch==2.8.0` desde el índice `cu128`).
- Extras `hf` de `pyproject.toml`: `transformers`, `peft`, `bitsandbytes`
  (QLoRA/NF4), `accelerate`, `grpcio`.
- Node 22 + dependencias del sidecar bitbeam (túneles e identidad BSV; `@bsv/sdk` requiere Node ≥22).

Variantes (build args):

```bash
docker build -f Dockerfile.gpu \
  --build-arg CUDA_IMAGE=nvidia/cuda:12.6.0-cudnn-runtime-ubuntu22.04 \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu126 \
  -t diloco-wan-gpu:cu126 .
```

## Flujo one-command con Compose

1. Arranca el coordinador (perfil `coordinator`) y copia la línea
   `didiloco://join?...` de sus logs:

   ```bash
   RUN_NAME=gpu-run PRESET=toy WORKERS=1 ROUNDS=2 \
     docker compose -f deploy/docker-compose.gpu.yml --profile coordinator up -d --build diloco-coordinator
   docker compose -f deploy/docker-compose.gpu.yml logs -f diloco-coordinator
   ```

   Usa `--advertise-host diloco-coordinator` para que el endpoint del invite
   sea alcanzable desde el contenedor del worker.

2. Pega la línea en `JOIN_CODE` y arranca el worker GPU:

   ```bash
   export JOIN_CODE='didiloco://join?h=http://diloco-coordinator:8095&t=...'
   NODE_ID=worker-gpu NO_TUNNEL=1 \
     docker compose -f deploy/docker-compose.gpu.yml up -d --build diloco-worker-gpu
   docker compose -f deploy/docker-compose.gpu.yml logs -f diloco-worker-gpu
   ```

Variables del worker:

| Variable | Default | Significado |
|---|---|---|
| `JOIN_CODE` | — | Código de unión; si está vacío se ejecuta el `CMD` por defecto. |
| `NODE_ID` | `worker-gpu` | Identidad/nombre del nodo. |
| `SHARD_ID` | `0` | Shard de datos. |
| `DEVICE` | `cuda` | Dispositivo (usa `cpu` para probar sin GPU). |
| `NO_TUNNEL` | `0` | `1` conecta directo al endpoint del invite (LAN/Compose). |
| `CUDA_IMAGE` | ver Dockerfile | Base CUDA. |
| `TORCH_VERSION` | `2.8.0` | Versión de PyTorch. |

## Ejecutar sin Compose

```bash
docker run --rm --gpus all \
  -e JOIN_CODE='didiloco://join?...' \
  -e NO_TUNNEL=1 \
  -e DEVICE=cuda \
  -v "$PWD/runs:/app/runs" \
  -v "$HOME/.cache/huggingface:/cache/hf" \
  diloco-wan-gpu:latest
```

## Verificación rápida

Imagen construida y verificada en un host con RTX 3080 / driver CUDA 13:
`torch 2.8.0+cu128`, `torch.cuda.is_available() == True`.

Además se verificó el flujo completo dentro del contenedor:
`pilot.py up --no-tunnel --advertise-host 127.0.0.1` → `docker run --gpus all
-e JOIN_CODE=... -e NO_TUNNEL=1 -e DEVICE=cuda` → worker admitido por invite,
`rounds_done=1`, ronda 0 completada en el coordinador.

```bash
docker run --rm --gpus all diloco-wan-gpu:latest \
  python3 -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

## Notas

- La imagen usa `/cache/hf` para el cache de Hugging Face (volumen `hf_cache`),
  así que los modelos se descargan una sola vez.
- `shm_size: 8gb` evita errores de memoria compartida con DataLoader.
- Para QLoRA 32B/70B usa `--preset 32b-qlora` / `70b-qlora` en el coordinador.
- El entrypoint (`scripts/docker_entrypoint_gpu.sh`) solo usa `JOIN_CODE` como
  argumento de `pilot.py join`; el token no se escribe en logs del proceso.
