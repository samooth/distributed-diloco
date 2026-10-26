# 01. Algoritmo: DiLoCo y variantes

Este documento explica el algoritmo, sus hiperparámetros, su coste de
comunicación y por qué se elige una variante concreta para 32B/70B sobre WAN.
No sustituye a los papers; los enlaces están en
[`09-referencias.md`](09-referencias.md).

## 1. Idea central

DiLoCo combina dos optimizadores:

- **Interior (inner):** cada isla entrena su copia del modelo durante `H` pasos
  con AdamW y su propio shard de datos. No hay comunicación entre islas.
- **Exterior (outer):** cada `H` pasos, las islas calculan un *pseudo-gradiente*
  (la diferencia entre el modelo global y el modelo local después de entrenar),
  lo agregan y aplican un paso de SGD con momento Nesterov.

Es una variante de *local SGD* / FedAvg con dos diferencias importantes:
muchos pasos locales (`H` grande) y un optimizador exterior con momento, que
estabiliza la agregación y mejora la convergencia frente a promediar parámetros
sin más.

```text
Modelo global θ^t
        │
        ├── Isla 1: H pasos AdamW ──► θ_1^t,H ──► Δ_1 = θ^t - θ_1^t,H
        ├── Isla 2: H pasos AdamW ──► θ_2^t,H ──► Δ_2 = θ^t - θ_2^t,H
        └── Isla M: H pasos AdamW ──► θ_M^t,H ──► Δ_M = θ^t - θ_M^t,H
                                              │
                                      Δ̄ = mean(Δ_i)
                                              │
                              θ^{t+1} = OuterOpt(θ^t, Δ̄)
```

## 2. Algoritmo formal

Notación:

| Símbolo | Significado |
|---|---|
| `θ^t` | Parámetros globales al inicio de la ronda `t` |
| `M` | Número de islas (workers DiLoCo) |
| `H` | Pasos locales entre sincronizaciones |
| `B` | Batch local por paso, en secuencias |
| `L` | Longitud de secuencia |
| `γ` | Learning rate interior (AdamW) |
| `η` | Learning rate exterior (Nesterov) |
| `μ` | Momento exterior |
| `N` | Número de parámetros que se sincronizan |
| `b` | Bytes por parámetro en el transporte |

Pseudocódigo:

```text
Inicializar θ^0 de forma idéntica en todas las islas
Inicializar v^0 = 0 (momento exterior)

para t = 0 .. T-1:
    publicar θ^t a todas las islas

    para cada isla i en paralelo:
        θ_i ← θ^t
        para h = 0 .. H-1:
            batch ← muestrear de su shard
            g ← ∇L(batch; θ_i)
            θ_i ← AdamW(θ_i, g, γ)
        Δ_i ← θ^t - θ_i

    Δ̄ ← (1/M) Σ_i Δ_i
    Δ̄ ← clip(Δ̄, max_norm)

    v^{t+1} ← μ v^t + Δ̄
    u^{t+1} ← Δ̄ + μ v^{t+1}
    θ^{t+1} ← θ^t - η u^{t+1}
```

La forma exacta del paso Nesterov debe ser la misma en todos los nodos. La
formulación de arriba coincide con `torch.optim.SGD(momentum=μ,
nesterov=True)`:

```text
buf ← μ buf + g
d   ← g + μ buf
θ   ← θ - η d
```

El signo de `Δ_i` importa. Si se define al revés (`θ_local - θ_global`), el
paso exterior cambia de signo y el entrenamiento diverge.

## 3. Por qué funciona con latencia alta

La comunicación ocurre una vez cada `H` pasos. Si una ronda tarda 40 minutos de
cómputo y la sincronización tarda 2-10 minutos, la latencia de red (decenas o
cientos de milisegundos) es irrelevante. Lo que importa es:

1. **Ancho de banda:** cuántos bytes hay que mover por ronda.
2. **Estabilidad:** que los nodos no desaparezcan a mitad de ronda.
3. **Stragglers:** el nodo más lento marca el final de la ronda.
4. **Tamaño del estado:** si se sincroniza el modelo completo, la ronda es
   enorme; si se sincroniza un adaptador, es pequeña.

DiLoCo no elimina la comunicación; la **amortiza**. Un modelo de 70B en bf16
sigue siendo un delta de 140 GB si se entrena full fine-tuning. La reducción
real viene de `H` y de entrenar solo los adaptadores.

## 4. Coste de comunicación

### 4.1 Fórmula general

Por isla y ronda, con un all-reduce en anillo:

```text
bytes_isla ≈ 2 · N · b · (M - 1) / M
```

Con un coordinador en estrella (subida del delta + bajada del global):

```text
bytes_isla ≈ 2 · N · b
bytes_coordinador ≈ 2 · M · N · b   (entrada + salida)
```

La comparación con data parallelism (DP) por paso:

```text
reducción ≈ H · (b_DP / b_DiLoCo)
```

Si DP usa bf16 y DiLoCo usa int8, la reducción se multiplica por 2. Si se usa
top-k al 1% y 2 bits, se multiplica por otros ~800.

### 4.2 Bytes por token

Para una isla con batch local `B` y secuencia `L`:

```text
tokens_por_ronda = H · B · L
bytes_por_token  ≈ 2 · N · b / (H · B · L)
```

Ejemplo: adaptador de 207M parámetros en bf16 (`N·b = 414 MB`), `H = 200`,
`B = 8`, `L = 1024`:

```text
tokens = 200 · 8 · 1024 = 1.638.400
bytes_por_token ≈ 828 MB / 1.638.400 ≈ 505 B/token
```

Comparado con el estado de activaciones y gradientes, 505 B/token es manejable
para una isla, pero sigue siendo un coste de subida que hay que presupuestar.

### 4.3 Tabla de referencia

Ver [`03-comunicacion.md`](03-comunicacion.md) para las tablas completas de
32B/70B con distintos rangos de LoRA y anchos de banda. Resumen:

| Caso | Parámetros sincronizados | bf16 por ronda (ida) |
|---|---:|---:|
| 70B, LoRA r=16 en todas las lineales | ~207 M | ~414 MB |
| 70B, LoRA r=16 solo atención | ~66 M | ~131 MB |
| 70B, LoRA r=16 solo q/v | ~33 M | ~66 MB |
| 32B, LoRA r=16 en todas las lineales | ~134 M | ~268 MB |
| 32B, LoRA r=16 solo atención | ~34 M | ~67 MB |
| 70B full fine-tuning | 70 B | ~140.000 MB |
| 32B full fine-tuning | 32 B | ~64.000 MB |

Las cifras de adaptadores son estimaciones para Llama-3.1-70B y Qwen2.5-32B;
la arquitectura exacta cambia el número. Lo importante es el orden de magnitud.

## 5. Hiperparámetros

| Hiperparámetro | Rango típico | Notas |
|---|---|---|
| `H` | 100-500 | Más `H` = menos comunicación, más riesgo de deriva local |
| `M` | 2-8 | Más islas = más datos por ronda; el batch óptimo crece con `M` |
| `γ` (inner LR) | 1e-4 a 4e-4 | Para LoRA, empezar en 1e-4; para preentrenamiento, 4e-4 |
| `η` (outer LR) | 0.2-1.0 | 0.7 es el valor de DiLoCo/OpenDiLoCo; barrer |
| `μ` | 0.9 | Momento Nesterov exterior |
| Batch local | 1-8 secuencias | Depende de VRAM; el batch global es `M × B` |
| Acumulación | 8-32 | Mantiene el batch efectivo sin subir VRAM |
| Warmup interior | 5-10% de pasos | AdamW es sensible al inicio |
| Weight decay interior | 0.01-0.1 | 0.1 en preentrenamiento; 0.01-0.05 en LoRA |
| Clip de pseudo-gradiente | 1-5 | Protege contra un nodo con pérdida anómala |

Reglas prácticas:

- **`H` no se elige por latencia, sino por la fracción de tiempo de
  sincronización.** Si la ronda de cómputo tarda `H · t_step` y la
  sincronización tarda `t_sync`, la fracción de sync es
  `t_sync / (H · t_step + t_sync)`. Buscar < 10-20%.
- **`H` demasiado grande degrada la calidad.** OpenDiLoCo vio que `H=500` no
  igualaba al baseline de 1.1B, mientras que `H=125` casi lo igualaba con el
  mismo presupuesto de cómputo.
- **`η` y `H` interactúan.** Si se aumenta `H`, el pseudo-gradiente acumula más
  desplazamiento local; conviene barrer `η` y no asumir 0.7.
- **El batch óptimo crece con `M`.** Los scaling laws de DiLoCo muestran que
  DiLoCo soporta batches globales más grandes que DP, lo que favorece la
  escalabilidad horizontal.

## 6. Evidencia empírica

| Trabajo | Escala | Resultado relevante | Limitación |
|---|---|---|---|
| DiLoCo (2023) | Hasta 400M, 8 workers, C4 | Igual o mejor que DP con ~500× menos comunicación | Modelos pequeños; `H=500` |
| OpenDiLoCo (2024) | 1.1B, 4 workers, 8×H100 por worker | 90-95% CU en 3 continentes; `H=125` casi iguala DP 4× batch; 125× menos comunicación | 4 workers; H=500 empeora |
| INTELLECT-1 (2024) | 10B, 1T tokens, hasta 14 nodos | 83-96% CU, 36-41% MFU, int8, `H=100` | Preentrenamiento; hardware H100 |
| Scaling Laws for DiLoCo (2025) | 35M-2.4B, validación 4B/10B | `M=2` supera a DP con >100× menos comunicación; el batch óptimo crece con `M` | `H=30`; no es fine-tuning |
| SparseLoCo (2025) | 178M-2B, 8-32 workers | 97-99% sparsity + 2 bits supera a DiLoCo denso | Preentrenamiento; no probado a 70B |
| Streaming DiLoCo (2025) | Billion-scale | 400× menos ancho de banda que DP; solapa comunicación y cómputo | Complejidad de implementación |
| DiLoCoX (2025) | 107B, 160×A800, 1 Gbps simulado | 357× throughput vs AllReduce; pérdida 4.20 vs 3.90 | Prueba de concepto con WikiText-103; no es producción |

Conclusión: DiLoCo es sólido para preentrenamiento de 1B-10B y para
fine-tuning de modelos grandes. Para 32B/70B **con adaptadores**, el régimen es
más fácil que el de los papers de preentrenamiento porque el delta es pequeño;
el riesgo principal es la calidad del fine-tuning y la estabilidad del outer
optimizer, no la comunicación.

## 7. Variantes y cuándo usarlas

### 7.1 DiLoCo denso (base)

- Sincroniza todos los parámetros entrenables en bf16/fp16 o int8.
- Simple, probado, fácil de depurar.
- **Uso:** v1 con LoRA/DoRA; full fine-tuning en islas de datacenter.

### 7.2 OpenDiLoCo / `prime`

- OpenDiLoCo es la implementación abierta de referencia; ya no se mantiene.
- `prime` es el framework de producción de Prime Intellect: `ElasticDeviceMesh`,
  FSDP2, int8 all-reduce, CPU offload, live checkpoint recovery, tolerancia a
  churn.
- **Uso:** referencia para full fine-tuning y para tomar ideas de tolerancia a
  fallos; para LoRA, un trainer propio es más simple.

### 7.3 Streaming DiLoCo

- Divide el modelo en fragmentos, sincroniza fragmentos en secuencia, solapa
  comunicación con cómputo y cuantiza a 4 bits.
- Reduce el pico de ancho de banda y el bloqueo; 400× menos que DP en los
  experimentos.
- **Uso:** full fine-tuning o modelos grandes donde el delta denso es el cuello
  de botella. No es la primera opción para adaptadores pequeños.

### 7.4 SparseLoCo

- Top-k por bloques (64×64 en matrices 2D, chunks de 4096 en 1D), 2 bits,
  error feedback, densidad 1-3%.
- Reemplaza el momento exterior por un acumulador de error feedback; con
  Nesterov ingenuo + top-k la calidad se degrada.
- Supera a DiLoCo denso en los experimentos de preentrenamiento, comunicando
  ~100× menos.
- **Uso:** full fine-tuning y preentrenamiento con ancho de banda extremo. Para
  un adaptador de 0.1-0.8 GB no compensa la complejidad inicial.

### 7.5 DeMo

- No es local-step: comprime el momento en cada paso con DCT + top-k y usa el
  propio buffer como error feedback.
- 85× menos datos por GPU en 300M; 44× en 1B (k=16).
- **Uso:** entrenamiento con pasos sincronizados pero ancho de banda limitado;
  no sustituye a DiLoCo cuando la latencia es alta y se quiere sincronizar poco.

### 7.6 MuLoCo

- Usa Muon como optimizador interior de DiLoCo; 2 bits con error feedback.
- 8× menos comunicación que DiLoCo con la misma memoria.
- **Uso:** variante prometedora para full fine-tuning; Muon no es estándar en
  todos los stacks y requiere cuidado.

### 7.7 DiLoCoX

- Pipeline parallelism + dual optimizer + one-step-delay overlap + compresión
  adaptativa.
- 107B sobre 1 Gbps simulado, 160 A800, 357× vs AllReduce.
- **Uso:** referencia para 100B+; no es una receta doméstica y su validación es
  una prueba de concepto.

### 7.8 Photon

- Federated averaging para preentrenamiento, hasta 7B, 64-512× menos
  comunicación, 35% mejor wall-time que baselines distribuidas.
- **Uso:** investigación; muestra que el preentrenamiento federado a 7B es
  posible, pero no es un framework maduro para 70B.

### 7.9 Factored Gossip DiLoCo

- Relaja la sincronización exacta a mezcla/gossip aproximada; degrada de forma
  gradual con retrasos y fallos; más robusto a stragglers.
- **Uso:** trabajo futuro para redes permissionless o muy inestables.

## 8. Matriz de decisión para este proyecto

| Requisito | DiLoCo denso | Streaming | SparseLoCo | DiLoCoX |
|---|---|---|---|---|
| 32B/70B con LoRA | **Sí, v1** | Overkill | Overkill | No |
| Full fine-tuning 7B-13B en islas | Sí | Sí | Sí | No |
| Full fine-tuning 32B/70B | Solo datacenter | Sí, mejor | Sí, mejor | Sí |
| 100B+ | No | Parcial | Parcial | **Sí** |
| Complejidad de implementación | Baja | Alta | Media | Muy alta |
| Madurez | Alta | Media | Media | Baja |
| Riesgo de calidad | Bajo con `H` moderado | Bajo | Medio (tuning EF) | Medio |

**Decisión:** v1 = DiLoCo denso sobre adaptadores. v1.1 = int8. v2 = evaluar
SparseLoCo o Streaming solo si se pasa a full fine-tuning o si el delta del
adaptador deja de ser pequeño. No se adopta DiLoCoX hasta que haya un caso de
100B+ con islas que lo justifiquen.

## 9. Lo que DiLoCo no resuelve

- **No es asíncrono.** Una isla lenta bloquea la ronda; hace falta deadline,
  expulsión o variantes asíncronas.
- **No reduce el coste de cómputo.** Cada isla entrena `H` pasos; el total de
  tokens procesados crece con `M`.
- **No garantiza privacidad.** Los nodos ven sus datos y los deltas; la
  privacidad requiere nodos de confianza, DP o agregación segura.
- **No verifica nodos.** Un nodo puede enviar un delta malicioso; hacen falta
  controles de integridad y reputación.
- **No elimina el tamaño del modelo.** Full fine-tuning de 70B sigue
  necesitando un estado enorme; el adaptador es lo que hace viable la WAN.

## 10. Preguntas abiertas

1. ¿Cuál es el `H` y `η` óptimos para LoRA de 32B/70B? Los valores de
   preentrenamiento no se trasladan directamente.
2. ¿Cómo afecta la heterogeneidad de datos entre islas al fine-tuning de un
   adaptador? DiLoCo es robusto en preentrenamiento, pero hay que medirlo.
3. ¿int8 es suficiente para adaptadores o hace falta error feedback?
4. ¿Merece la pena DoRA en un régimen distribuido, dado que añade parámetros de
   magnitud y cómputo?
5. ¿Cómo se comporta el outer optimizer con adaptadores de rango bajo y
   diferentes shards?
6. ¿Se puede usar un coordinador en estrella con 8 islas o hace falta
   anillo/árbol desde el principio?

Estas preguntas se convierten en ablaciones en
[`08-experimentos.md`](08-experimentos.md).
