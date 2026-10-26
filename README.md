# DiLoCo WAN: adaptación distribuida de LLMs sobre internet

**Estado:** documentación v0.1 + implementación P0/P1/P2.1 (2026-10-26).
Incluye backend toy y Hugging Face/PEFT, coordinador HTTP, checkpoints,
reanudación, compresión int8 y late join opcional. 32B/70B reales y el
endurecimiento P2+ siguen pendientes.

Objetivo: adaptar modelos de **32B, 70B o superiores** usando varios nodos
conectados por internet (fibra doméstica, WAN, nodos heterogéneos), sin exigir
una interconexión de datacenter. El método base es **DiLoCo** (*Distributed
Low-Communication*), con sincronización infrecuente y un optimizador exterior de
tipo Nesterov. Para que 32B/70B quepan en nodos domésticos, la ruta principal es
**QLoRA/LoRA**: la base queda congelada y solo se sincronizan los adaptadores.

> La latencia no es el problema principal. Lo son el **ancho de banda de subida**,
> la **estabilidad de los nodos** y la **memoria de la GPU**. DiLoCo amortiza la
> latencia porque solo sincroniza cada `H` pasos locales, pero sigue enviando un
> delta del tamaño de lo que se entrena en cada ronda.

## Decisión en una frase

Entrenar **adaptadores LoRA/DoRA de 32B/70B** con **DiLoCo denso** (bf16/fp16,
luego int8), sincronizando cada 100-500 pasos locales entre **islas** que
replican la base cuantizada; dejar el entrenamiento de todos los parámetros y el
preentrenamiento de 70B para islas de datacenter o para una fase posterior con
Streaming DiLoCo / SparseLoCo / DiLoCoX.

## Estado de implementación

Completado en P0/P1 y parcialmente en P2:

- Configuración validada con fingerprint de modelo, datos y entrenamiento.
- Backend `toy` determinista para tests de CPU y backend `hf` con PEFT/QLoRA.
- Solo se serializan parámetros entrenables; la base no se envía por la red.
- Coordinador HTTP/1.0 con token, quórum, deadline, heartbeat, agregación
  Nesterov y checkpoints atómicos.
- Reanudación desde checkpoint y exportación previa del adaptador inicial para
  que el coordinador no cargue la base de 32B/70B.
- Smoke test de 2 workers y validación end-to-end con Qwen2.5-0.5B en CPU.
- Baseline LoRA centralizado (`scripts/train_centralized.py`) con el mismo
  presupuesto de pasos/tokens para medir el `loss gap`.
- Compresión int8 por tensor del delta del adaptador (`train.delta_compression`).
- Rejoin del mismo `node_id`, late join opcional (`coordinator.allow_late_join`)
  y reintento automático si el worker termina una ronda obsoleta.
- Carga thread-safe del modelo HF (`_MODEL_LOAD_LOCK`) para evitar meta-tensors
  cuando dos workers cargan el modelo a la vez en CPU.

Límites actuales: HTTP/1.0 + token (sin TLS/mTLS), sin top-k ni error feedback,
sin expulsión automática de nodos ni presupuesto de churn, y sin ejecución real
de 32B/70B. La validación HF se hizo con Qwen2.5-0.5B en CPU; no sustituye a un
piloto grande.

## Qué es viable y qué no

| Escenario | Viabilidad en nodos domésticos | Camino |
|---|---|---|
| 32B QLoRA, 1 GPU de 24-48 GB por isla | Sí, con cuidado | Base NF4 + LoRA, `seq_len` 512-1024, checkpointing |
| 70B QLoRA, 1 GPU de 48-80 GB por isla | Sí, 48 GB es el mínimo ajustado | Base NF4 + LoRA r=8-16, batch 1, checkpointing |
| 70B QLoRA, 2-4 GPUs de 24 GB por isla | Muy lento; no recomendado | Offload a CPU o FSDP; mejor 48 GB+ |
| 32B full fine-tuning | No | 8-16 × 80 GB como mínimo |
| 70B full fine-tuning | No | 16-32 × 80 GB; no es un caso de fibra doméstica |
| Preentrenamiento 70B desde cero | No | Islas de datacenter; DiLoCoX para 100B+ |
| 100B+ con pipeline parallelism | Solo investigación | DiLoCoX / islas con interconexión local |

## Decisiones clave

| Tema | Decisión v1 | Motivo |
|---|---|---|
| Algoritmo | DiLoCo: AdamW interior + Nesterov exterior | Simple, probado, tolera latencia y datos no-IID |
| Parámetros que se sincronizan | Solo adaptador LoRA/DoRA | Reduce la ronda de 140 GB a 0.07-0.8 GB en 70B |
| Precisión de la base | NF4 con doble cuantización, congelada | 70B ≈ 35-40 GB; 32B ≈ 17-20 GB |
| Precisión del delta | bf16/fp16 en v1; int8 implementado en P2.1 | El adaptador ya es pequeño; primero corregir, luego comprimir |
| Topología | Estrella con coordinador para 2-4 islas; anillo/árbol para más | La estrella es simple; el coordinador se satura con muchos nodos |
| Sincronización | Rondas síncronas con deadline y checkpoint por ronda | Fácil de razonar y recuperar; expulsión automática pendiente |
| Nodos | Permitidos (allowlist) en v1 | La verificación de cómputo permissionless no está resuelta |
| Stack | Python + PyTorch + Transformers + PEFT + bitsandbytes | Es el ecosistema real de QLoRA/FSDP; Zig no aporta aquí |
| Modelo | Denso primero; MoE después | El repo ya tiene MoE, pero el routing complica la sincronización |

## Topología

```text
                    ┌─────────────────────────────┐
                    │ Coordinador                 │
                    │ - membership / leases       │
                    │ - agregación y outer step   │
                    │ - checkpoints y manifiestos │
                    │ - métricas / autenticación  │
                    └──────────────┬──────────────┘
                                   │  HTTP/1.0 (P1) → gRPC/HTTP2+TLS (P2)
              ┌────────────────────┼────────────────────┐
              │                    │                    │
       ┌──────▼──────┐      ┌──────▼──────┐      ┌──────▼──────┐
       │ Isla A      │      │ Isla B      │      │ Isla C      │
       │ 1-8 GPUs    │      │ 1-8 GPUs    │      │ 1-8 GPUs    │
       │ base NF4    │      │ base NF4    │      │ base NF4    │
       │ LoRA local  │      │ LoRA local  │      │ LoRA local  │
       └─────────────┘      └─────────────┘      └─────────────┘

Isla = unidad de sincronización DiLoCo. Puede ser 1 GPU o un nodo multi-GPU
con DDP/FSDP local. El coordinador puede ser también una isla, pero no es
recomendable para cargas grandes.
```

## Una ronda, paso a paso

1. El coordinador publica `RoundStart` con la versión global del adaptador, el
   hash de la base, la config y un deadline.
2. Cada isla carga la versión global (o ya la tiene), entrena `H` pasos locales
   con AdamW sobre su shard de datos y calcula
   `Δ_i = θ_global - θ_local_final`.
3. Cada isla sube `Δ_i` comprimido y firmado; el coordinador valida hash,
   ronda, shape y norma.
4. El coordinador agrega `Δ̄ = mean(Δ_i)`, aplica Nesterov exterior y obtiene
   `θ_global'`.
5. El coordinador publica `θ_global'` (o el delta) y el manifiesto de la ronda;
   cada isla actualiza su copia y empieza la ronda siguiente.
6. Cada `N` rondas se evalúa en un holdout y se guarda checkpoint. El checkpoint
   global es pequeño si solo contiene el adaptador.

## Requisitos

### Hardware

- **Por isla, ruta 32B:** 1 GPU de 24 GB (ajustado) o 48 GB (recomendado).
- **Por isla, ruta 70B:** 1 GPU de 48 GB (ajustado) u 80 GB (recomendado); 2-4
  GPUs de 48 GB para DDP del adaptador si se quiere más throughput.
- **RAM:** 64 GB por isla como mínimo para 32B; 128 GB para 70B.
- **Disco:** 100 GB libres para base + adaptadores + checkpoints; 300 GB para
  trabajar cómodo con 70B.
- **Red:** fibra con subida estable. 50 Mbps de subida es usable con adaptadores
  pequeños; 200-500 Mbps simplifica mucho las rondas de 70B con LoRA completo.

### Software

- Python 3.11-3.13, PyTorch 2.8+, Transformers 5.x, PEFT 0.17+, bitsandbytes,
  accelerate, safetensors, datasets.
- P1 usa la biblioteca estándar: HTTP/1.0, JSON y safetensors. No requiere
  gRPC ni FastAPI.
- P2.1 (int8) usa safetensors con tensores `int8` y una escala `fp32` por
  tensor. P2.2 añade late join opcional y reintento de ronda obsoleta; gRPC/HTTP2,
  TLS/mTLS, top-k/error feedback y object store (MinIO/S3) siguen pendientes.
- Contenedores con versiones fijas de CUDA, PyTorch y bitsandbytes. La versión
  de bitsandbytes debe ser idéntica en todas las islas.

### Red

- WireGuard/Tailscale/Netbird para NAT traversal y mTLS; evitar exponer puertos
  del coordinador directamente a internet.
- Medir con `iperf3` y `mtr`; no asumir que el ancho de banda contratado es el
  efectivo. La subida doméstica suele ser asimétrica.
- NTP activo. Relojes sincronizados para logs y deadlines.

## Stack: por qué Python y no Zig

El repositorio se llama `moe-zig-lab` porque el dominio de datos es Zig, no
porque el entrenamiento esté implementado en Zig. El ecosistema práctico para
QLoRA, PEFT, bitsandbytes, FSDP2 y checkpoints está en Python/PyTorch. Zig
podría usarse para el agente de nodo, el CLI o el transporte, pero no es la
prioridad y añadiría riesgo en la fase de validación. La decisión se revisa si
el cuello de botella pasa a ser el transporte o la orquestación.

## Plan de implementación

| Fase | Qué se construye | Criterio de salida |
|---|---|---|
| P0 | Especificación, config, esqueleto del repo | Hecho: docs y config validada |
| P1 | Backend toy/HF, coordinador HTTP, 2 islas, checkpoints, reanudación | Hecho en localhost; HF 0.5B validado en CPU |
| P2 | int8 y late join/reintento hechos; gRPC/mTLS, top-k y churn pendientes | Ronda WAN con fallos inyectados |
| P3 | 4 islas WAN emulada (`tc netem`) + baseline centralizado | Convergencia y utilización medidas |
| P4 | Piloto 32B QLoRA | Calidad y throughput aceptables |
| P5 | Piloto 70B QLoRA | Calidad y throughput aceptables |
| P6 | Endurecimiento: seguridad, stragglers, churn | Runbook probado con fallos inyectados |

El detalle está en [`docs/08-experimentos.md`](docs/08-experimentos.md).

## Inicio rápido (P1)

```bash
cd distributed-diloco

# Tests de CPU (toy, protocolo, checkpoints, 2 islas)
../hf-venv/bin/python -m unittest discover -s tests -v

# Smoke de 2 islas en un solo proceso (4 rondas)
../hf-venv/bin/python scripts/smoke_local.py --reset

# F1 toy: 20 rondas x H=20 x 2 workers = 800 pasos-equivalentes
../hf-venv/bin/python scripts/smoke_local.py \
  --config configs/f1-toy.json --reset --rounds 20
../hf-venv/bin/python scripts/train_centralized.py \
  --config configs/f1-toy.json --device cpu

# La misma prueba con delta int8
../hf-venv/bin/python scripts/smoke_local.py \
  --config configs/int8-toy.json --reset --rounds 20
```

Modo con procesos separados:

```bash
# Terminal 1
../hf-venv/bin/python scripts/run_coordinator.py \
  --config configs/smoke.json --checkpoint-dir runs/process --no-resume --max-rounds 4

# Terminal 2 y 3 (copiar el PORT que imprime el coordinador)
../hf-venv/bin/python scripts/run_worker.py --config configs/smoke.json \
  --node-id node-0 --shard-id 0 --coordinator http://127.0.0.1:PORT \
  --log runs/process/logs/node-0.jsonl
../hf-venv/bin/python scripts/run_worker.py --config configs/smoke.json \
  --node-id node-1 --shard-id 1 --coordinator http://127.0.0.1:PORT \
  --log runs/process/logs/node-1.jsonl
```

Para HF, exportar primero el adaptador inicial y arrancar el coordinador con
`--init-state`; así el coordinador no carga la base:

```bash
../hf-venv/bin/python scripts/export_initial_state.py \
  --config configs/qwen-0.5b-single.json --out runs/init-qwen --device cuda

../hf-venv/bin/python scripts/run_coordinator.py \
  --config configs/qwen-0.5b-single.json --init-state runs/init-qwen \
  --checkpoint-dir runs/qwen-0.5b-single --max-rounds 1

../hf-venv/bin/python scripts/run_worker.py \
  --config configs/qwen-0.5b-single.json --node-id node-0 --shard-id 0 \
  --coordinator http://127.0.0.1:PORT --device cuda
```

`configs/qwen-0.5b-single.json` apunta a un modelo local y a
`../data/zig_dense_v5_module_clean.jsonl`; ajustar rutas, `max_length`,
`max_samples` y `coordinator.token` antes de usarlo. En esta máquina la GPU
puede estar ocupada: usar `--device cpu` para una prueba funcional lenta.

## Documentación

| Documento | Contenido |
|---|---|
| [`docs/01-algoritmo.md`](docs/01-algoritmo.md) | DiLoCo, pseudo-gradientes, hiperparámetros y variantes |
| [`docs/02-arquitectura.md`](docs/02-arquitectura.md) | Componentes, topologías, estados de ronda y tolerancia a fallos |
| [`docs/03-comunicacion.md`](docs/03-comunicacion.md) | Protocolo, compresión, presupuestos de ancho de banda y transporte |
| [`docs/04-modelos.md`](docs/04-modelos.md) | Memoria de 32B/70B, QLoRA, LoRA, FSDP y configuraciones |
| [`docs/05-entrenamiento.md`](docs/05-entrenamiento.md) | Receta, datos, evaluación, checkpoints y recetas concretas |
| [`docs/06-operaciones.md`](docs/06-operaciones.md) | Despliegue, monitorización, runbook, capacidad y costes |
| [`docs/07-seguridad.md`](docs/07-seguridad.md) | Modelo de amenazas, controles, privacidad y legal |
| [`docs/08-experimentos.md`](docs/08-experimentos.md) | Hipótesis, fases, ablaciones y criterios de aceptación |
| [`docs/09-referencias.md`](docs/09-referencias.md) | Papers, repos y enlaces comentados |
| [`docs/10-glosario.md`](docs/10-glosario.md) | Términos en español/inglés y acrónimos |

## Métricas que importan

- **Loss gap** frente a un baseline centralizado con los mismos tokens y datos.
- **Compute utilization (CU):** fracción del tiempo de GPU que se pasa entrenando.
- **Goodput:** tokens que contribuyen a rondas aceptadas por segundo.
- **Bytes por token:** bytes de sincronización / tokens locales de la ronda.
- **Tiempo hasta el objetivo:** wall-clock para alcanzar una pérdida de validación.
- **Tasa de stragglers y de churn:** porcentaje de rondas con nodos expulsados.
- **Tiempo de recuperación:** desde un fallo hasta la siguiente ronda válida.

## Riesgos principales

| Riesgo | Impacto | Mitigación |
|---|---|---|
| La subida doméstica satura el coordinador | Rondas lentas | Anillo/árbol, int8, adaptadores q/v, H mayor |
| 70B no cabe en 48 GB con `seq_len` alto | OOM | `seq_len` 512-1024, batch 1, checkpointing, 80 GB |
| Un nodo lento bloquea la ronda | Baja utilización | Deadline, expulsión, rejoin al final de ronda |
| Base distinta entre nodos | Pseudo-gradientes inválidos | Hash de base y config; fallar cerrado |
| Datos no-IID o de baja calidad | Peor convergencia | Shards estratificados, mezcla global, evaluación |
| Nodo malicioso envenena el adaptador | Modelo dañado | Allowlist, clipping, auditoría, robustez v2 |
| Divergencia por `H`/LR exterior | Run perdido | Sweep, rollback, checkpoint frecuente |

## Estado y siguientes pasos

P0/P1 están implementados y probados en localhost. El baseline centralizado
también está implementado. En F1 toy (`H=20`, `outer_lr=0.2`, 20 rondas), con
800 pasos-equivalentes:

- DiLoCo: `eval_loss=0.003723`
- Baseline centralizado: `eval_loss=0.008095`
- `loss_gap = -54%` (DiLoCo mejor en este toy)

Con int8 por tensor la pérdida del toy fue `0.003339` (sin degradación
apreciable frente a `0.003723`). El payload de Qwen2.5-0.5B bajó de 2.16 MB a
0.54 MB (4×); el mensaje HTTP completo fue 0.567 MB (3.8×). Esto valida el
circuito de comparación, pero **no es F1 real**: falta el barrido con datos Zig,
Qwen2.5-0.5B/1.5B y dos nodos. La config `smoke.json` original
(`outer_lr=0.7`) no estaba tuneada y daba un gap grande.

Siguientes pasos:

1. Repetir F1 con Qwen2.5-0.5B, holdout Zig y dos nodos; `H=10` dio
   `loss_gap=+5.26%` (inicial 2.5002, DiLoCo 2.4178, baseline 2.2971). Probar
   `H ∈ {20, 50, 100}`, int8 y 3 semillas; guardar curvas y `loss_gap`.
2. Ejecutar F1 int8 con `configs/f1-qwen-0.5b-int8.json` y repetir el
   barrido de `H`; el código y el test unitario ya están listos.
3. Completar P2.2: gRPC/mTLS, top-k/error feedback, expulsión automática de
   stragglers y tolerancia a churn.
3. Probar 4 islas con WAN emulada (`tc netem`) y medir goodput, CU y bytes por
   token.
4. Solo después, alquilar hardware para el piloto 32B y luego 70B.

## Referencias esenciales

- DiLoCo: <https://arxiv.org/abs/2311.08105>
- OpenDiLoCo: <https://arxiv.org/abs/2407.07852>
- INTELLECT-1: <https://arxiv.org/abs/2412.01152>
- Streaming DiLoCo: <https://arxiv.org/abs/2501.18512>
- SparseLoCo: <https://arxiv.org/abs/2508.15706>
- DiLoCoX: <https://arxiv.org/abs/2506.21263>

La lista completa, con notas sobre qué aporta cada trabajo y sus limitaciones,
está en [`docs/09-referencias.md`](docs/09-referencias.md).
