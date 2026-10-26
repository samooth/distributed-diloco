# 05. Receta de entrenamiento

Esta es la receta operativa para adaptar 32B/70B con DiLoCo + QLoRA. Los
valores son puntos de partida; cada run debe medir y ajustar. La regla es no
cambiar más de una cosa a la vez.

## 1. Pipeline

```text
1. Congelar base y verificar hash
2. Inyectar LoRA/DoRA
3. Cargar shard de datos y holdout
4. Inicializar adaptador global (semilla fija)
5. Repetir por ronda:
   a. cada isla entrena H pasos locales
   b. calcula Δ = θ_global - θ_local
   c. sube Δ; el coordinador agrega y aplica Nesterov
   d. publica θ_global'
   e. cada N rondas: evaluar y checkpoint
6. Exportar adaptador; evaluar sin fusionar y fusionado
```

## 2. Hiperparámetros de partida

| Parámetro | 32B QLoRA | 70B QLoRA | Notas |
|---|---:|---:|---|
| `H` | 100-200 | 100-200 | Subir a 500 si el sync domina |
| `M` | 4-8 | 4-8 | Menos islas = menos datos por ronda |
| `seq_len` | 1024 | 512-1024 | 70B en 48 GB: 512 |
| `micro_batch` | 1 | 1 | VRAM |
| `grad_accum` | 8-16 | 16-32 | Batch efectivo |
| `inner_lr` | 1e-4 | 1e-4 | AdamW; barrer 5e-5 a 2e-4 |
| `outer_lr` | 0.7 | 0.7 | Nesterov; barrer 0.2-1.0 |
| `outer_momentum` | 0.9 | 0.9 | |
| `weight_decay` | 0.01-0.05 | 0.01-0.05 | Solo adaptador |
| `grad_clip` | 1.0 | 1.0 | Clip interior |
| `pseudo_grad_clip` | 1-5 | 1-5 | Clip exterior |
| `warmup` | 5-10% | 5-10% | Pasos interiores |
| `lora_r` | 16 | 8-16 | Todas o atención |
| `lora_alpha` | 32 | 16-32 | 2×r |
| `lora_dropout` | 0.05 | 0.05 | 0 para determinismo |
| `epochs` | 1-3 | 1-2 | Depende del dataset |
| `eval_every` | 20-50 rondas | 20-50 rondas | Holdout fijo |

### 2.1 Batch global

```text
batch_global = M · micro_batch · grad_accum
tokens_por_ronda = H · batch_global · seq_len
```

Ejemplo: `M=4`, `micro_batch=1`, `grad_accum=16`, `H=200`, `seq_len=1024`:

```text
batch_global = 64 secuencias
tokens_por_ronda = 200 · 64 · 1024 = 13.1 M tokens
```

Ese número es el total procesado por el run en una ronda; cada isla procesa
`H · 16 · 1024 = 3.28 M` tokens. Si el dataset tiene 100 M tokens, una ronda
consume el 13% del dataset; hay que barajar y repetir con cuidado.

### 2.2 Learning rates

- **Interior:** AdamW sobre el adaptador. Para LoRA, `1e-4` es un buen inicio;
  `2e-4` puede funcionar en datasets pequeños; `5e-5` si hay inestabilidad.
- **Exterior:** Nesterov sobre pseudo-gradientes. `0.7` es el valor de los
  papers de preentrenamiento; en LoRA puede ser alto. Barrer
  `{0.2, 0.4, 0.7, 1.0}`.
- **Warmup exterior:** no hay warmup estándar; el momento exterior arranca en
  cero. Si hay picos al inicio, considerar clip o un warmup corto de `η`.
- **Interacción:** si se duplica `H`, el desplazamiento local se duplica; el
  outer LR efectivo también. No mantener `η` ciegamente.

## 3. Datos

### 3.1 Requisitos

- Manifest con hash, número de filas, tokens y shards.
- Sin solape con el holdout. Usar decontaminación por hash exacto y por
  similitud/overlap de salida, como en el resto del repo.
- Cada isla ve solo su shard. El coordinador no necesita ver el contenido.
- Shards estratificados por familia de tareas para evitar que una isla vea solo
  un tipo de dato.
- Barajado reproducible por ronda: `seed = hash(run_id, round, shard_id)`.
- Tokens de padding enmascarados; EOS al final; loss completion-only para SFT.

### 3.2 Estrategias de sharding

| Estrategia | Ventaja | Riesgo | Uso |
|---|---|---|---|
| IID aleatorio | Simple; DiLoCo robusto | Puede desbalancear familias | v1 |
| Estratificado | Cobertura por familia | Más lógica | Recomendado |
| Por nodo / local | Privacidad; sin transferencia | Datos no-IID; sesgo | Red permissionless |
| Por ronda | Cobertura global | Más índice; cuidado con repetición | Runs largos |

DiLoCo es robusto a datos no-IID en preentrenamiento, pero no se debe abusar:
si una isla solo ve código Zig y otra solo prosa, el adaptador puede sesgarse.
Mezclar y evaluar.

### 3.3 Piloto Zig

El repo ya tiene datos y una métrica de compilación real:

- `data/zig_dense_v5_module_clean.jsonl` (train limpio).
- `data/zig_holdout.jsonl` (holdout de módulos).
- `moe_zig.eval` (compile real y CE completion-only).

Pero el volumen es pequeño (~1.5-2.5k filas; millones de tokens, no miles de
millones). Sirve para:

- Validar el pipeline distribuido de punta a punta.
- Medir compile rate de un adaptador 32B/70B frente a 0.5B/7B.
- Detectar regresiones y sobreajuste.

No sirve para demostrar una mejora general de 70B. Para eso hace falta un
corpus mayor y con licencia adecuada. No mezclar el holdout en ningún shard.

## 4. Pérdida y evaluación

### 4.1 Pérdida

- SFT: cross-entropy completion-only, token-weighted.
- Preentrenamiento: next-token CE; considerar z-loss para estabilidad.
- Reportar pérdida media por ronda y por isla; no solo la del coordinador.
- Registrar `grad_norm`, `pseudo_grad_norm`, `update_norm` y norma por isla.

### 4.2 Evaluación

- Holdout fijo, congelado antes de empezar.
- Evaluar siempre con el mismo `seq_len`, batch y plantilla.
- Métricas:
  - CE completion-only.
  - Métrica de tarea (compile rate para Zig; exact match para otras).
  - Benchmarks generales para detectar olvido catastrófico.
  - `hit_cap` / longitud media si aplica.
- Frecuencia: cada 20-50 rondas; no evaluar en cada ronda si cuesta más que
  entrenar.
- Evaluar el adaptador **sin fusionar** y, al final, también fusionado.
- No usar el holdout para elegir hiperparámetros y luego reportarlo como test
  final; congelar un test adicional si se va a afirmar una mejora.

### 4.3 Qué comparar

| Baseline | Para qué |
|---|---|
| LoRA centralizado en 1 GPU | Calidad de referencia con los mismos tokens |
| DDP/FSDP centralizado en LAN | Techo de throughput y calidad |
| DiLoCo `M=1` | Aísla el efecto del outer optimizer |
| DiLoCo denso bf16 vs int8 | Coste/beneficio de compresión |
| `H` sweep | Comunicación vs calidad |
| Adaptador q/v vs atención vs todas | Coste vs calidad |

## 5. Outer optimizer

Implementación recomendada en v1: **centralizada en el coordinador**.

```text
1. Recibir Δ_i de cada isla.
2. Validar shape, dtype, hash, norma.
3. Δ̄ = mean(Δ_i) en fp32.
4. Δ̄ = clip(Δ̄, max_norm).
5. v = μ·v + Δ̄
6. u = Δ̄ + μ·v
7. θ_global = θ_global - η·u
8. Publicar θ_global.
```

Ventajas: una sola copia del estado, sin drift entre islas, auditoría simple.
Desventajas: el coordinador necesita el adaptador completo en CPU/GPU y ancho
de banda de salida. Para adaptadores de 0.03-0.8 GB es manejable.

Alternativa para v2: outer replicado en cada isla. Cada isla aplica el mismo
paso sobre su copia; no hay que bajar el adaptador, solo el Δ̄. Requiere
determinismo y validación de que todas las copias coinciden (hash periódico).

## 6. Checkpoints y reanudación

### 6.1 Qué guardar

- Adaptador global (`safetensors`) y hash.
- Estado del outer optimizer (momento `v`).
- Ronda, `run_id`, `config_hash`, `base_hash`, `data_manifest_hash`.
- Estado del sampler (ronda, seed, posición por shard).
- Métricas de la ronda.
- Versión de código (`git commit`) y versiones de librerías.

### 6.2 Cuándo

- Cada ronda: adaptador + outer state (pequeño).
- Cada `N` rondas: checkpoint completo del adaptador y evaluación.
- Full fine-tuning: checkpoint shardeado cada `N` rondas; retención corta.
- Nunca a mitad de ronda; si se guarda, marcar como `partial` y no usarlo.

### 6.3 Reanudación

- Cargar el checkpoint global y el estado del outer.
- Las islas descargan el adaptador y reinician AdamW (o cargan su estado local
  si existe).
- Reanudar en la ronda `r+1`; descartar cualquier delta de la ronda abortada.
- Verificar hashes antes de entrenar.

## 7. Reproducibilidad

- Semilla global y semillas por isla/ronda.
- Orden de datos determinista por shard y ronda.
- Misma versión de PyTorch, CUDA, bitsandbytes y PEFT en todas las islas.
- Registrar `torch.__version__`, `transformers.__version__`,
  `peft.__version__`, `bitsandbytes.__version__`, driver y GPU.
- Aceptar que GPU heterogéneas no son bit-exactas; buscar reproducibilidad
  estadística (misma curva, no mismos bits).
- Guardar el adaptador final y los checkpoints intermedios.

## 8. Depuración

### 8.1 Test de equivalencia

Antes de escalar:

1. Modelo diminuto (2-4 capas), dataset sintético.
2. `M=1`, `H=1`: debe coincidir con un entrenamiento LoRA normal.
3. `M=2`, mismos shards y misma semilla: el promedio de deltas idénticos debe
   coincidir con `M=1`.
4. `M=2`, shards distintos, `H=10`: comparar pérdida con un baseline de batch
   global equivalente; no se espera bit-exactitud, sí curva similar.
5. Repetir con int8 y verificar que el error de cuantización es pequeño.

### 8.2 Síntomas y causas

| Síntoma | Causa probable | Acción |
|---|---|---|
| Pérdida plana o sube | `η` alto, `H` alto, LR alto | Bajar `η`/`γ`; reducir `H` |
| Pico al inicio | Warmup insuficiente, datos malos | Warmup; revisar batches |
| Pérdida distinta por isla | Datos no-IID o LR | Estratificar; revisar |
| `pseudo_grad_norm` enorme | Un nodo divergió | Clip; excluir nodo; rollback |
| NaN | fp16 overflow, LR alto | bf16; bajar LR; clip |
| Throughput cae con las rondas | Fragmentación, memoria, swap | Reiniciar worker; `expandable_segments` |
| Sync domina | `H` bajo, adaptador grande, red mala | Subir `H`; int8; anillo |
| Un nodo siempre tarde | GPU/red/CPU lenta | Expulsar; rejoin; nodo dedicado |
| Calidad peor que baseline | `H`/`η`/datos | Ablación; volver a config conocida |

### 8.3 Señales de un nodo malo

- Norma del delta muy distinta al resto.
- Pérdida local mucho más baja (posible overfit o datos filtrados).
- Pérdida local mucho más alta (datos corruptos).
- Cosine similarity de su delta con la media < 0.
- Tiempo de ronda anómalo.
- Hash de base/config distinto.

## 9. Recetas concretas

### 9.1 32B QLoRA, 4 islas × 1 GPU de 48 GB

```yaml
run_id: zig-32b-qlora-diloco-v1
model:
  base: Qwen/Qwen2.5-32B-Instruct
  quantization: nf4_double
  lora: {r: 16, alpha: 32, dropout: 0.05, target: all_linear}
algorithm:
  name: diloco
  local_steps: 200
  outer_optimizer: nesterov
  outer_lr: 0.7
  outer_momentum: 0.9
  pseudo_grad_clip: 2.0
training:
  seq_len: 1024
  micro_batch: 1
  grad_accum: 16
  inner_lr: 1.0e-4
  weight_decay: 0.02
  grad_clip: 1.0
  epochs: 2
data:
  manifest: data/manifest-zig-32b.json
  shards: 4
sync:
  topology: star
  compression: bf16
  deadline_s: 1800
checkpoint:
  every_rounds: 1
  eval_every_rounds: 25
```

### 9.2 70B QLoRA, 8 islas × 1 GPU de 48 GB

```yaml
run_id: zig-70b-qlora-diloco-v1
model:
  base: meta-llama/Llama-3.1-70B-Instruct
  quantization: nf4_double
  lora: {r: 8, alpha: 16, dropout: 0.05, target: attention}
algorithm:
  name: diloco
  local_steps: 200
  outer_optimizer: nesterov
  outer_lr: 0.5
  outer_momentum: 0.9
  pseudo_grad_clip: 2.0
training:
  seq_len: 512
  micro_batch: 1
  grad_accum: 32
  inner_lr: 1.0e-4
  weight_decay: 0.02
  grad_clip: 1.0
  epochs: 1
data:
  manifest: data/manifest-70b.json
  shards: 8
sync:
  topology: ring
  compression: int8
  deadline_s: 3600
checkpoint:
  every_rounds: 1
  eval_every_rounds: 25
```

### 9.3 7B full fine-tuning, islas de 8×80 GB (opcional)

```yaml
model:
  base: meta-llama/Llama-3.1-8B
  quantization: none
  fsdp: full_shard
algorithm:
  local_steps: 100
  outer_lr: 0.7
training:
  seq_len: 4096
  micro_batch: 1
  grad_accum: 16
  inner_lr: 3.0e-4
sync:
  compression: int8
  topology: ring
```

Este caso sirve para validar el modo sharded y el full fine-tuning antes de
intentar 32B/70B, no para producción doméstica.

## 10. Definición de “run terminado”

- Adaptador final guardado y hasheado.
- Evaluación completa en holdout con la misma config que el baseline.
- Curva de pérdida por ronda y por isla.
- Métricas de comunicación: bytes, tiempo, fracción de sync.
- Métricas de utilización: CU, goodput, stragglers.
- Config, hashes y versiones registrados.
- Si se afirma una mejora, comparación pareada contra el baseline y un test no
  usado para tuning.

## 11. Errores comunes

- Empezar con 70B en vez de 0.5B/1.5B.
- Cambiar `H`, `η` y datos a la vez.
- No fijar la base o el tokenizer.
- Usar `seq_len` distinto entre islas.
- Evaluar con el holdout contaminado.
- No guardar el estado del outer optimizer.
- Reanudar con AdamW reseteado sin documentarlo.
- Fusionar el adaptador en NF4.
- Ignorar el coste de la evaluación (puede dominar el wall-clock).
- Medir throughput sin incluir sync y esperas.
- Reportar el mejor checkpoint sin decir cuántas veces se miró el holdout.
