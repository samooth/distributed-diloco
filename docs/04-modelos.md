# 04. Modelos, memoria y configuración de 32B/70B

Este documento traduce “32B/70B sobre fibra doméstica” a números de VRAM,
tamaño de adaptador, configuraciones de QLoRA y límites reales. La conclusión
anticipada: **32B/70B con QLoRA son viables; full fine-tuning no**.

## 1. La ecuación de memoria

Para un modelo con `P` parámetros:

```text
pesos          = P · bytes_por_parametro
gradientes     = P · bytes_por_gradiente
optimizador    = P · bytes_por_estado
activaciones   = f(batch, seq_len, checkpointing, arquitectura)
workspace      = kernels, fragmentación, buffers
VRAM ≈ suma de todo lo anterior
```

Bytes por parámetro habituales:

| Formato | Pesos | Gradientes | AdamW (m + v + master) | Total aproximado |
|---|---:|---:|---:|---:|
| bf16 + AdamW mixto | 2 | 2 | 12 | **16 B/param** |
| bf16 + AdamW bf16 | 2 | 2 | 6 | 10 B/param |
| LoRA bf16 + AdamW fp32 | 2 | 2 | 12 | **16 B/param del adaptador** |
| NF4 (QLoRA) | 0.5 + overhead | 0 (base congelada) | 0 | **~0.5-0.6 B/param** |

La base NF4 no tiene gradientes ni estados de AdamW. Solo el adaptador los
tiene. Por eso QLoRA cambia el problema: de 1.12 TB (70B full) a ~35-40 GB de
base + ~3 GB de adaptador/optimizador.

## 2. Números base

| Modelo | Pesos bf16 | Pesos NF4 | Full FT (16 B/param) | LoRA r=16 todas las lineales |
|---|---:|---:|---:|---:|
| 32B | 64 GB | ~16-20 GB | ~512 GB | ~134 M params (~0.42%) |
| 70B | 140 GB | ~35-40 GB | ~1.12 TB | ~207 M params (~0.30%) |

Notas:

- NF4 incluye overhead de cuantización y embeddings que suelen quedar en bf16;
  por eso el rango es amplio.
- “Full FT” es el estado de entrenamiento, no solo los pesos. No cabe en una
  GPU doméstica ni en 8×24 GB.
- El porcentaje de LoRA es sobre el total; el adaptador es pequeño pero no
  gratis: su optimizador AdamW en fp32 pesa más que el propio adaptador.

## 3. Adaptadores: tamaño y coste

Fórmula para una capa lineal con `d_in` entradas y `d_out` salidas:

```text
params_LoRA = r · (d_in + d_out)
```

Sumando todas las capas objetivo:

| Modelo | Objetivo | r | Params | Peso bf16 | AdamW fp32 (aprox.) |
|---|---|---:|---:|---:|---:|
| 70B | todas las lineales | 8 | 103.5 M | 207 MB | ~1.2 GB |
| 70B | todas las lineales | 16 | 207.1 M | 414 MB | ~2.5 GB |
| 70B | todas las lineales | 32 | 414.2 M | 828 MB | ~5.0 GB |
| 70B | solo atención | 16 | 65.5 M | 131 MB | ~0.8 GB |
| 70B | solo q, v | 16 | 32.8 M | 66 MB | ~0.4 GB |
| 32B | todas las lineales | 8 | 67.1 M | 134 MB | ~0.8 GB |
| 32B | todas las lineales | 16 | 134.2 M | 268 MB | ~1.6 GB |
| 32B | todas las lineales | 32 | 268.4 M | 537 MB | ~3.2 GB |
| 32B | solo atención | 16 | 33.6 M | 67 MB | ~0.4 GB |
| 32B | solo q, v | 16 | 16.8 M | 34 MB | ~0.2 GB |

El coste de AdamW se estima con 12 B/param (master fp32 + m + v) más peso y
gradiente. Si se usa `paged_adamw_8bit`, baja; si se mantiene todo en fp32,
sube. Medir siempre con `torch.cuda.max_memory_allocated()`.

### 3.1 Qué objetivo elegir

| Objetivo | Calidad esperada | Coste de sync | Uso |
|---|---|---|---|
| q, v | La más baja | Mínimo | Prototipo, redes muy lentas |
| Atención (q,k,v,o) | Media | Bajo | Piloto 70B con subida limitada |
| Todas las lineales | La más alta | Alto | Calidad; requiere buena subida o anillo |
| MLP solamente | Media | Medio | Caso específico; no recomendado como base |

Para 70B en fibra doméstica, empezar por **atención** o **q/v** para validar el
sistema; luego pasar a todas las lineales si la red lo permite. Para 32B, todas
las lineales es viable con 100 Mbps y `H` alto.

## 4. Modos de isla y VRAM

### 4.1 Isla de 1 GPU

| GPU | 32B QLoRA | 70B QLoRA |
|---|---|---|
| 8 GB | No | No |
| 12 GB | No | No |
| 16 GB | No (o con offload extremo) | No |
| 24 GB | Ajustado: `seq_len` 512-1024, batch 1, checkpointing | No |
| 32 GB | Viable con cuidado | No |
| 48 GB | Cómodo | Ajustado: `seq_len` 1024, batch 1, checkpointing |
| 80 GB | Muy cómodo | Cómodo |

La GPU local del repo es una RTX 3080 Laptop de 8 GB: sirve para el smoke test
de 0.5B, no para 32B/70B. Los pilotos grandes deben correr en otros nodos.

### 4.2 Isla multi-GPU replicada (recomendado para QLoRA)

Cada GPU carga la base completa y una copia del adaptador; DDP del adaptador
dentro de la isla.

| Modelo | Configuración mínima | Recomendado |
|---|---|---|
| 32B | 4×24 GB (ajustado) | 2×48 GB |
| 70B | 2×48 GB (ajustado) | 4×48 GB u 2×80 GB |

Ventaja: no hay que shardear la base; el adaptador se sincroniza por DDP local
cada paso y solo se envía un delta DiLoCo por isla. Desventaja: la base debe
caber en cada GPU.

### 4.3 Isla sharded (full fine-tuning)

FSDP2/ZeRO-3 reparte pesos, gradientes y estados. El pseudo-gradiente se
calcula por shard y se agrega por shard entre islas.

| Modelo | Full FT mínimo realista | Cómodo |
|---|---|---|
| 32B | 8×80 GB (ajustado) | 16×80 GB |
| 70B | 16×80 GB (muy ajustado) | 32×80 GB |

Con CPU offload se puede reducir el número de GPUs, pero el throughput cae y la
complejidad sube. No es una ruta doméstica.

## 5. Configuración QLoRA recomendada

### 5.1 Base

```python
bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=torch.bfloat16,
)

model = AutoModelForCausalLM.from_pretrained(
    base_path,
    quantization_config=bnb_config,
    device_map={"": 0},          # una GPU; evitar auto si se puede
    torch_dtype=torch.bfloat16,
    attn_implementation="sdpa",  # o "flash_attention_2" si está soportado
)
model = prepare_model_for_kbit_training(
    model,
    use_gradient_checkpointing=True,
)
```

Reglas:

- `device_map="auto"` puede repartir capas entre GPU y CPU; es cómodo para
  inferencia, pero para entrenamiento puede degradar mucho el throughput.
  Preferir una GPU o un shard explícito.
- `use_gradient_checkpointing=True` es obligatorio para 70B en 48 GB.
- `torch.backends.cuda.matmul.allow_tf32 = True` y
  `torch.backends.cudnn.allow_tf32 = True` en Ampere+.
- `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` ayuda con fragmentación.
- Fijar `attn_implementation` según la GPU; no asumir FlashAttention en todas.

### 5.2 LoRA

```python
lora_config = LoraConfig(
    r=16,
    lora_alpha=32,
    lora_dropout=0.05,
    bias="none",
    task_type="CAUSAL_LM",
    target_modules=[
        "q_proj", "k_proj", "v_proj", "o_proj",
        "gate_proj", "up_proj", "down_proj",
    ],
)
```

Variantes:

- `r=8, alpha=16` para 70B en 48 GB si el optimizador del adaptador aprieta.
- `r=16, alpha=32` como base.
- `r=32, alpha=64` si hay VRAM y se busca más capacidad; el delta de sync
  crece.
- `use_rslora=True` para escalado por rango; probar.
- `use_dora=True` para DoRA; añade parámetros de magnitud y cómputo. No
  combinarlo con rutas MoE de PEFT que usan `ParamWrapper`.
- `lora_dropout=0` si se busca determinismo; 0.05 para regularizar.

### 5.3 Optimizador y precisión

- AdamW fp32 para el adaptador; `paged_adamw_8bit` si falta VRAM, sabiendo que
  puede cambiar la reproducibilidad.
- `bf16` en Ampere+; `fp16` + loss scaling en GPUs antiguas.
- Acumulación de gradientes 8-32; no reducir el batch efectivo sin ajustar LR.
- Clip de gradiente 1.0.
- `max_length` 1024 como punto de partida; 512 para 70B en 48 GB.
- Evaluación con `use_cache=False` y batch 1.

### 5.4 Configuraciones de referencia

**32B, 1×48 GB:**

```yaml
base: Qwen/Qwen2.5-32B-Instruct
quantization: nf4 + double_quant
lora: {r: 16, alpha: 32, dropout: 0.05, target: all_linear}
seq_len: 1024
micro_batch: 1
grad_accum: 16
optimizer: adamw_bf16
gradient_checkpointing: true
```

**70B, 1×48 GB (ajustado):**

```yaml
base: meta-llama/Llama-3.1-70B-Instruct
quantization: nf4 + double_quant
lora: {r: 8, alpha: 16, dropout: 0.05, target: attention}
seq_len: 512
micro_batch: 1
grad_accum: 32
optimizer: paged_adamw_8bit
gradient_checkpointing: true
```

**70B, 1×80 GB (cómodo):**

```yaml
base: meta-llama/Llama-3.1-70B-Instruct
quantization: nf4 + double_quant
lora: {r: 16, alpha: 32, dropout: 0.05, target: all_linear}
seq_len: 1024
micro_batch: 1
grad_accum: 16
optimizer: adamw_bf16
gradient_checkpointing: true
```

Estas configs son puntos de partida; la VRAM real depende de la arquitectura,
del tokenizer, del padding y de la implementación de atención. Medir.

## 6. Full fine-tuning: por qué no

| Modelo | Estado mínimo | GPUs de 80 GB | Comentario |
|---|---:|---:|---|
| 32B | ~512 GB | 8 (ajustado) | No cabe en nodos domésticos |
| 70B | ~1.12 TB | 16 (muy ajustado) | 32 es lo cómodo |
| 70B + activaciones | >1.2 TB | 16-32 | No es un caso de fibra doméstica |

Además, full fine-tuning sincroniza 64-140 GB por ronda. A 100 Mbps son horas
por dirección. La combinación de memoria y red lo descarta para este
experimento. Si se necesita, es un caso de islas de datacenter con FSDP2 y,
posiblemente, Streaming DiLoCo/SparseLoCo.

## 7. MoE

El repo ya tiene un Qwen0.6B-MoE y scripts para expertos. Para 32B/70B MoE:

- La base completa debe caber igual; los expertos inactivos también ocupan
  memoria.
- Los adaptadores pueden ir en atención y/o expertos; PEFT tiene rutas
  específicas (`ParamWrapper`) y limitaciones (por ejemplo, no DoRA).
- El routing introduce no-determinismo y hace que distintos nodos activen
  expertos distintos; el delta del adaptador puede ser disperso.
- La sincronización densa del adaptador sigue siendo posible, pero el análisis
  de convergencia es más difícil.
- **Decisión:** denso primero. MoE es una fase posterior, no v1.

## 8. Fusionar y servir

- No fusionar un adaptador dentro de una base NF4. Cargar la base en bf16 en
  una máquina con RAM/VRAM suficiente y usar `merge_and_unload()`.
- 70B bf16 para fusionar requiere ~140 GB de pesos + overhead; usar CPU con
  suficiente RAM o una GPU de 80 GB con descarga por capas.
- Alternativa: servir el adaptador por separado (PEFT, vLLM con LoRA, LoRAX).
  Es lo recomendado para no duplicar 140 GB.
- Evaluar siempre el adaptador **sin fusionar** y, si se fusiona, volver a
  evaluar; una fusión mal hecha puede degradar.
- Para GGUF, convertir desde la base bf16 fusionada; el adaptador NF4 no es una
  base válida para conversión directa.

## 9. Compatibilidad

| Arquitectura | Notas |
|---|---|
| Llama 3.x | Bien soportada por PEFT/bitsandbytes; 70B es el caso de referencia |
| Qwen2.5 | Bien soportada; 32B es el caso de referencia |
| Mistral/Mixtral | Densa y MoE; cuidado con expertos |
| Modelos con `trust_remote_code` | Evitar en nodos no confiables; auditar el código |
| Modelos multimodales | Fuera de alcance v1 |
| MoE | Fase posterior; routing y adaptadores por experto |

## 10. Hoja de cálculo de VRAM

```text
VRAM_base      = P · 0.5 GB/param (NF4) + embeddings_bf16 + overhead
VRAM_adapter   = P_adapter · 16 B/param
VRAM_activ    ≈ 1-4 GB con gradient checkpointing (seq 512-1024, batch 1)
VRAM_workspace ≈ 1-2 GB
VRAM_total     = suma
```

Ejemplo 70B, r=8 atención:

```text
base ≈ 35-40 GB
adapter ≈ 65.5M · 16 B ≈ 1.0 GB
activaciones ≈ 2-4 GB
workspace ≈ 1-2 GB
total ≈ 39-47 GB  → 48 GB muy ajustado; 80 GB cómodo
```

Ejemplo 32B, r=16 todas:

```text
base ≈ 17-20 GB
adapter ≈ 134M · 16 B ≈ 2.1 GB
activaciones ≈ 2-4 GB
workspace ≈ 1-2 GB
total ≈ 22-28 GB  → 24 GB ajustado; 48 GB cómodo
```

Si el total se acerca al límite, reducir en este orden:

1. `seq_len` (1024 → 512).
2. Rango de LoRA (16 → 8).
3. Objetivo (todas → atención → q/v).
4. Optimizador paginado.
5. `micro_batch` (ya suele ser 1).
6. Offload (último recurso; cae el throughput).

## 11. Errores comunes

- Contar solo los pesos y olvidar el optimizador del adaptador.
- Asumir que NF4 deja la base “gratis”: sigue ocupando 35-40 GB en 70B.
- Usar `device_map="auto"` y terminar con capas en CPU sin saberlo.
- Fusionar el adaptador en NF4 y perder calidad.
- Elegir r=64 “porque hay VRAM” y multiplicar el coste de sincronización.
- Entrenar 70B en 24 GB con offload y esperar throughput utilizable.
- No fijar la revisión del modelo y cargar una base distinta en cada nodo.
- Mezclar `fp16` y `bf16` entre islas; los deltas no son comparables.
