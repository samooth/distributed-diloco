# 08. Plan de experimentos

Este documento convierte el diseño en una secuencia de experimentos con
hipótesis, métricas y criterios de parada. La regla es **no escalar de tamaño
hasta que la fase anterior sea aburridamente estable**.

## 1. Hipótesis

| ID | Hipótesis | Cómo se falsifica |
|---|---|---|
| H1 | Un adaptador LoRA entrenado con DiLoCo converge a menos del 3-5% de un LoRA centralizado con los mismos tokens | Pérdida muy superior o divergencia |
| H2 | La latencia WAN (20-300 ms) no domina si `H ≥ 100`; el cuello es ancho de banda y stragglers | Sync alto con latencia baja; poca mejora al bajar latencia |
| H3 | int8 en el delta del adaptador no degrada de forma medible | Pérdida/calidad peor que bf16 más allá del ruido |
| H4 | 32B/70B QLoRA caben y entrenan en 24/48 GB con `seq_len` 512-1024 | OOM, throughput inutilizable o calidad pobre |
| H5 | Existe un `H` intermedio (100-300) que minimiza tiempo hasta el objetivo | `H` bajo domina por sync; `H` alto degrada convergencia |
| H6 | Expulsar stragglers con deadline mejora el goodput sin dañar la calidad | Goodput no mejora o la pérdida empeora |
| H7 | Para adaptadores, SparseLoCo/Streaming no aportan frente a int8 denso | El delta ya es pequeño; el ahorro no compensa |
| H8 | 32B/70B distribuido mejora el compile rate de Zig frente a 0.5B/7B | No hay mejora fuera del ruido del holdout |

H1-H7 son de sistema; H8 es de dominio y solo se puede afirmar con un holdout
grande y limpio. El holdout actual del repo es pequeño y se ha reutilizado; para
H8 hace falta congelar un test adicional.

## 2. Métricas

### 2.1 Calidad

- **Pérdida de validación** en un holdout fijo, con la misma config y tokenizer.
- **Loss gap** frente al baseline:

```text
loss_gap = (L_diloco - L_baseline) / L_baseline
```

- **Métrica de tarea:** compile rate real para Zig; exact match/F1 para otras.
- **Olvido catastrófico:** benchmarks generales antes/después.
- **Estabilidad:** desviación de la pérdida por ronda; picos; NaN.

### 2.2 Sistema

| Métrica | Definición | Objetivo |
|---|---|---|
| Compute utilization (CU) | tiempo de GPU entrenando / wall-clock | > 80% |
| Goodput | tokens de rondas confirmadas / wall-clock | maximizar |
| Sync fraction | tiempo de sync / tiempo de ronda | < 20% |
| Bytes por token | bytes de sync / tokens confirmados | minimizar |
| Time-to-target | wall-clock hasta pérdida objetivo | comparar |
| Straggler ratio | p95 de ronda / mediana | < 1.3 |
| Recovery time | fallo → siguiente ronda válida | < 1 ronda |
| Peak VRAM | máximo por GPU | < 95% |
| Churn | joins+leaves / 100 rondas | < 5 en v1 |
| Coste | €/1B tokens o €/run | comparar con alquiler |

### 2.3 Fórmulas

```text
CU_isla = t_gpu_entrenando / t_wall
CU_global = media ponderada por tokens
goodput = tokens_confirmados / t_wall
sync_fraction = t_sync / (t_compute + t_sync + t_wait)
bytes_por_token = bytes_sync_totales / tokens_confirmados
time_to_target = t en alcanzar L* en el holdout
```

`t_wait` incluye stragglers y timeouts. No ocultar la espera dentro de
“comunicación”.

## 3. Fases

| Fase | Nodos | Modelo | Objetivo | Duración | Criterio de salida |
|---|---|---|---|---|---|
| F0 | 1×8 GB | 0.5B QLoRA | Pipeline, test de equivalencia | 1-2 días | Ronda completa; checkpoint/reload |
| F1 | 2 | 0.5B/1.5B | DiLoCo vs centralizado; LAN + netem | 3-5 días | Loss gap ≤5%; sync ≤20% |
| F2 | 4 | 7B/8B QLoRA | WAN real; int8; fallos | 1-2 semanas | Loss gap ≤5%; CU ≥70%; recovery ≤2 rondas |
| F3 | 4-8 | 32B QLoRA | 24/48 GB; calidad | 2-4 semanas | 32B distribuido ≈ 32B single-node (±3%) |
| F4 | 4-8 | 70B QLoRA | 48/80 GB; coste | 4-8 semanas | 70B distribuido ≈ 70B single-node (±3%) |
| F5 | 2-4 islas | 7B/13B full FT | FSDP2 + DiLoCo | 2-4 semanas | Loss gap ≤5%; sync ≤20% |
| F6 | 8-20 | 32B/70B full o 100B+ | Investigación | No planificado | Solo con caso y presupuesto |

El hardware local (RTX 3080 Laptop 8 GB) solo sirve para F0. F1 requiere dos
máquinas; F2-F4 requieren nodos con 24-80 GB.

### 3.1 Estado de implementación (2026-10-26)

P0/P1 están implementados:

- Config validada, backend toy y backend HF/PEFT, protocolo binario con
  safetensors, checkpoints atómicos, reanudación y smoke de 2 workers.
- F0 parcial: se validó una ronda end-to-end con Qwen2.5-0.5B en CPU, se probó
  la reanudación desde checkpoint y el delta agregado. La equivalencia de un
  paso entre backend local y baseline centralizado está cubierta por tests.
  int8, top-k y error feedback están implementados; falta un barrido de varias
  rondas con dos nodos. P2.2 añade late join, salida ordenada, expulsión básica
  y TLS opcional; faltan gRPC/mTLS real y pruebas de churn con fallos inyectados.
  La carga HF usa un lock para evitar meta-tensors con dos workers en CPU.
- El baseline centralizado está implementado en
  `scripts/train_centralized.py`. En F1 toy (`configs/f1-toy.json`, `H=20`,
  `outer_lr=0.2`, 20 rondas y 800 pasos-equivalentes): baseline
  `eval_loss=0.008095`, DiLoCo `eval_loss=0.003723`, `loss_gap=-54%`. Es una
  validación del circuito, no un resultado de F1 real: falta Qwen2.5-0.5B/1.5B,
  holdout Zig y dos nodos. La config `smoke.json` (`outer_lr=0.7`) no estaba
  tuneada y mostraba un gap grande.
- F1 parcial ejecutada en CPU con Qwen2.5-0.5B-Instruct, 2 workers, datos Zig,
  `H=10`, `outer_lr=0.2`, 5 rondas (50 pasos-equivalentes) y 16 ejemplos de
  holdout: inicial `eval_loss=2.500206`; DiLoCo final `2.417826`; baseline
  centralizado final `2.297088`; `loss_gap=+5.26%`, ligeramente por encima del
  umbral H1 de 3-5%. Sync medio 0.61 s/ronda, sync fraction ≈2.5%, bytes por
  ronda ≈2.16 MB.
- F1 int8 ejecutada en GPU con Qwen2.5-0.5B-Instruct, 2 workers, `H=10`,
  `outer_lr=0.2`, 5 rondas: inicial `2.491262`, DiLoCo `2.418675`, baseline
  `2.297088`, `loss_gap=+5.29%`. Upload por ronda `567341-567342 B` frente a
  `2162688 B` de delta (3.8x).
- F1 int8+top-k al 10% ejecutada en GPU con Qwen2.5-0.5B-Instruct, 2 workers,
  `H=10`, `outer_lr=0.2`, 5 rondas y error feedback: inicial `2.491262`,
  DiLoCo `2.474486`, baseline `2.297088`, `loss_gap=+7.72%`. Upload por ronda
  `544057-544059 B` (4.0x). Queda repetir con `H ∈ {20, 50, 100}` y 3 semillas.
- F2-F6 no ejecutadas. La GPU local estaba ocupada durante el desarrollo, así
  que la validación HF fue en CPU y no sustituye a un piloto 32B/70B.

## 4. F0: pipeline y equivalencia

### Objetivo

Demostrar que la implementación es correcta antes de distribuir.

### Config

- Modelo 0.5B (Qwen2.5-0.5B-Instruct) en 8 GB, 4-bit o bf16.
- Dataset sintético pequeño + una muestra del dataset Zig.
- `M=1`, `H=1`, `seq_len=512`, `micro_batch=1`, `grad_accum=1`.
- Luego `M=2` en el mismo proceso (dos workers simulados) con shards idénticos.

### Pruebas

1. `M=1`, `H=1`: el paso debe coincidir con LoRA estándar (misma semilla,
   mismos datos, mismo optimizer).
2. `M=2`, shards idénticos: el delta promedio debe coincidir con `M=1`.
3. `M=2`, shards distintos, `H=10`: pérdida finita y comparable a un baseline
   de batch global equivalente.
4. Guardar/cargar checkpoint: reanudar y comparar la siguiente ronda.
5. int8 roundtrip: error de cuantización acotado; pérdida no explota.
6. Test de signo: invertir el signo del pseudo-gradiente debe divergir; el test
   debe detectarlo.

### Criterio de salida

- Todos los tests pasan.
- Una ronda de 10 pasos guarda adaptador, outer state y métricas.
- El adaptador recargado produce la misma pérdida en el holdout.

## 5. F1: dos islas y WAN emulada

### Objetivo

Medir el efecto de `H`, la latencia y la compresión en un modelo pequeño.

### Config

- 0.5B o 1.5B, QLoRA.
- `M=2`; `H ∈ {20, 50, 100, 200}`; `seq_len=512`; batch global equivalente.
- Topología estrella.
- Red: LAN y `tc netem` con latencia 20/100/300 ms, pérdida 0/0.5/1%, ancho de
  banda 20/100 Mbps.
- Compresión: bf16 vs int8.
- 3 semillas para las configuraciones clave.

### Medidas

- Pérdida de validación cada 20 rondas.
- Tiempo de ronda, compute, sync, espera.
- Bytes/token.
- Estabilidad (picos, NaN).
- Calidad final vs baseline centralizado con los mismos tokens.

### Criterio de salida

- Loss gap ≤5% con la mejor config.
- Sync ≤20% con `H` elegido.
- int8 no empeora más de 1% la pérdida.
- Latencia 20 ms vs 300 ms no cambia el resultado de forma significativa
  (confirma H2).
- Si no se cumple, no pasar a F2.

## 6. F2: cuatro islas, 7B/8B, red real

### Objetivo

Validar el sistema en condiciones reales: WAN, nodos heterogéneos, fallos.

### Config

- 7B/8B QLoRA, `seq_len=1024`, batch 1, grad accum 16.
- `M=4`; `H=100-200`; `η` barrido inicial.
- int8; topología estrella (si el adaptador es pequeño) o anillo (si es
  grande).
- Dataset: 10-50M tokens; Zig si es piloto, público si es general.
- Inyectar: un nodo 30% más lento; matar un nodo a mitad de ronda; perder red
  30 s; disco lleno simulado.

### Medidas

- Loss gap vs 4×DDP en LAN y vs single-node.
- CU, goodput, sync, stragglers.
- Recovery time y rondas perdidas.
- VRAM y throughput por GPU.
- Coste de tráfico.

### Criterio de salida

- Loss gap ≤5%.
- CU ≥70%; sync ≤20%.
- Recuperación ≤2 rondas.
- Un nodo caído no corrompe el run.
- int8 estable.

## 7. F3: 32B QLoRA

### Objetivo

Demostrar que 32B es viable en nodos de 24-48 GB y medir el coste real.

### Config

- Qwen2.5-32B-Instruct, NF4, LoRA r=16 todas las lineales.
- Islas de 1×48 GB (recomendado) o 2×24 GB (ajustado).
- `seq_len=1024`, batch 1, grad accum 16.
- `M=4-8`, `H=100-200`, `η=0.5-0.7`, int8.
- 50-200M tokens para el piloto; 1B si hay presupuesto.

### Medidas

- Throughput por GPU y por isla.
- VRAM pico; OOM; estabilidad.
- Loss gap vs 32B single-node LoRA con los mismos tokens.
- Calidad Zig (compile) vs baseline 7B/0.5B.
- Coste por 1B tokens.

### Criterio de salida

- 32B distribuido dentro de ±3% de pérdida del 32B single-node.
- Compile rate no peor que el single-node; si hay mejora, reportarla con
  holdout limpio.
- Throughput suficiente para terminar el presupuesto en el tiempo previsto.
- Si 32B no mejora a 7B en la tarea, replantear el caso de uso antes de 70B.

## 8. F4: 70B QLoRA

### Objetivo

Medir si 70B es viable en 48-80 GB y si la calidad justifica el coste.

### Config

- Llama-3.1-70B-Instruct, NF4, LoRA r=8-16.
- Islas de 1×48 GB (atención, seq 512) o 1×80 GB (todas, seq 1024).
- `M=4-8`, `H=100-200`, int8, anillo si el delta es grande.
- 100-500M tokens para el piloto.

### Medidas

- Throughput, VRAM, OOM, tokens/s por €.
- Loss gap vs 70B single-node en 80 GB.
- Calidad de tarea vs 32B.
- Tiempo hasta el objetivo y coste.
- Tasa de stragglers y de churn.

### Criterio de salida

- 70B distribuido dentro de ±3% del 70B single-node.
- Coste por 1B tokens comparable a alquilar un nodo de 80 GB o mejor.
- Si 70B no supera a 32B en la tarea, no escalar; el coste no se justifica.

## 9. F5 (opcional): full fine-tuning 7B/13B

### Objetivo

Validar el modo sharded y el full fine-tuning con FSDP2 + DiLoCo antes de
pensar en 32B/70B full.

### Config

- 7B/13B, bf16, FSDP2 dentro de la isla (8×80 GB), DiLoCo entre islas.
- `H=100`, int8, anillo.
- Dataset 1-10B tokens.

### Criterio

- Loss gap ≤5% vs DDP centralizado.
- Sync ≤20%.
- Checkpoint shardeado y reanudación correctos.

## 10. Matriz de ablaciones

| Variable | Valores | Pregunta |
|---|---|---|
| `H` | 20, 50, 100, 200, 500 | ¿Dónde está el mejor coste/calidad? |
| `M` | 2, 4, 8 | ¿Escala la calidad y el throughput? |
| `η` | 0.2, 0.4, 0.7, 1.0 | ¿El valor de preentrenamiento sirve para LoRA? |
| `γ` | 5e-5, 1e-4, 2e-4 | ¿LR interior óptimo? |
| Compresión | bf16, int8, 4-bit | ¿Cuánto se puede comprimir sin perder? |
| Adaptador | q/v, atención, todas | ¿Coste/calidad? |
| Rango | 8, 16, 32 | ¿Capacidad vs sync? |
| Datos | IID, estratificado, no-IID | ¿Robustez a heterogeneidad? |
| Topología | estrella, anillo | ¿Coordinador o red balanceada? |
| Fallos | ninguno, lento, caída, churn | ¿Goodput y recuperación? |
| Optimizador | LoRA, DoRA | ¿Merece la pena DoRA? |
| Base | NF4, bf16 | ¿La cuantización afecta la calidad? |

No ejecutar todas las combinaciones. Elegir las que respondan a una decisión:
las primeras cinco son obligatorias; el resto, según aparezcan cuellos.

## 11. Presupuesto de tokens y tiempo

### 11.1 Tokens

| Fase | Tokens | Comentario |
|---|---:|---|
| F0 | < 1 M | Sintético + muestra |
| F1 | 5-20 M | Suficiente para ver curvas |
| F2 | 20-100 M | 7B/8B |
| F3 | 50-200 M | 32B piloto |
| F4 | 100-500 M | 70B piloto |
| F5 | 1-10 B | Full FT, solo si hay presupuesto |

El dataset Zig actual (millones de tokens) alcanza para F0-F2; para F3/F4 hay
que ampliar con datos públicos o del usuario. Repetir el dataset no sustituye
diversidad.

### 11.2 Throughput de planificación

Rangos de orden de magnitud, **no benchmarks**; medir en F2/F3:

| Modelo | GPU | tokens/s por GPU (aprox.) |
|---|---|---:|
| 0.5B QLoRA | 8 GB | 3.000-8.000 |
| 7B/8B QLoRA | 24 GB | 800-2.000 |
| 32B QLoRA | 48 GB | 200-600 |
| 70B QLoRA | 48 GB | 80-250 |
| 70B QLoRA | 80 GB | 150-400 |

Ejemplo: 70B, 8 islas, 150 tokens/s por isla, eficiencia 60%:

```text
throughput ≈ 8 · 150 · 0.6 = 720 tokens/s
100 M tokens ≈ 100e6 / 720 ≈ 139.000 s ≈ 38.6 h
500 M tokens ≈ 193 h ≈ 8 días
```

El sync y los stragglers pueden duplicar el wall-clock. No comprar hardware
basándose en esta tabla; usarla solo para descartar configuraciones.

## 12. Protocolo experimental

1. Congelar baseline y holdout antes de la primera ronda.
2. Fijar datos, tokenizer, `seq_len` y batch global entre comparaciones.
3. Cambiar una variable a la vez.
4. 3 semillas en F0/F1; 1-2 en F3/F4 por coste, reportando la limitación.
5. Reportar media y rango, no solo el mejor punto.
6. Registrar config/hashes y no reutilizar `run_id`.
7. No mirar el holdout para tuning y luego reportarlo como test.
8. Guardar curvas y logs crudos, no solo el resumen.
9. Si un resultado es sorprendente, repetirlo antes de creerlo.
10. Documentar los runs fallidos; son parte del resultado.

## 13. Plantilla de resultado

```yaml
run_id:
date:
phase: F1
git_commit:
base: Qwen/Qwen2.5-0.5B-Instruct
base_hash:
config_hash:
data_manifest_hash:
M:
H:
outer_lr:
outer_momentum:
compression: bf16
topology: star
tokens_seen:
wall_clock_h:
loss_final:
loss_baseline:
loss_gap:
task_metric:
compile_rate:
compute_utilization:
goodput_tokens_s:
sync_fraction:
bytes_per_token:
straggler_ratio:
recovery_rounds:
peak_vram_gb:
notes:
```

## 14. Criterios de parada

- F0 no pasa: no se distribuye nada.
- F1 loss gap > 10% o divergencia: revisar algoritmo/datos antes de escalar.
- F1 sync > 40%: rediseñar topología o reducir adaptador.
- F2 CU < 60% o recovery > 5 rondas: el sistema no es operativo.
- F3 32B no mejora a 7B en la tarea: replantear el caso de uso.
- F4 70B no mejora a 32B: no escalar.
- Coste por 1B tokens > alquiler de un nodo equivalente: usar alquiler.
- Cualquier incidente de integridad no explicado: pausar y auditar.

## 15. Riesgos experimentales

| Riesgo | Mitigación |
|---|---|
| Dataset demasiado pequeño | No afirmar mejoras generales; usar como piloto |
| Holdout contaminado | Decontaminar; congelar test adicional |
| Un solo seed | Repetir configs clave |
| Hardware heterogéneo | Reportar por nodo; no comparar tokens/s absolutos |
| Evaluación cara | Evaluar cada N rondas; presupuestar |
| Cambios de librería | Fijar versiones; container |
| Sesgo de selección | Preregistrar criterios; reportar todos los runs |
| Métricas incompletas | Loggear sync, espera, stragglers |

## 16. Qué falsificaría el enfoque

- Que el loss gap de DiLoCo con adaptadores sea > 10% de forma sistemática.
- Que el sync de 70B no baje del 50% ni con int8/anillo/H alto.
- Que 70B QLoRA no quepa o sea 10× más lento que 32B sin mejora de calidad.
- Que la calidad de 32B/70B no supere a 7B en la tarea de interés.
- Que el coste total supere al alquiler de un nodo grande.
- Que un nodo malicioso pueda degradar el modelo sin ser detectado y sin
  posibilidad de rollback.

Si se cumple cualquiera, el experimento no justifica producción en su forma
actual; habría que cambiar de algoritmo, de escala o de caso de uso.

## 17. Próximos pasos

1. Implementar F0 (trainer + tests).
2. Implementar coordinador mínimo y ronda de 2 islas simuladas.
3. Ejecutar F1 con `tc netem`.
4. Publicar resultados internos antes de F2.
5. No hablar de 70B hasta que F1 y F2 estén verdes.
