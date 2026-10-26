# 10. Glosario

Términos en español, su equivalente en inglés y una definición operativa.

## A

**AdamW**
Optimizador con momento y segundo momento, con weight decay desacoplado. Es el
optimizador interior de DiLoCo.

**Adaptador (adapter)**
Conjunto pequeño de parámetros añadidos a un modelo congelado. En este proyecto,
LoRA/DoRA. Es lo único que se sincroniza entre islas.

**Agregación (aggregation)**
Combinar los deltas de varias islas en uno. En v1, media en fp32 en el
coordinador.

**All-reduce**
Operación colectiva que combina un valor de todos los procesos y lo devuelve a
todos. En un anillo, cada proceso envía/recibe ~2N bytes.

**Anillo (ring)**
Topología en la que cada nodo se comunica con dos vecinos. Balancea el ancho de
banda, pero un nodo lento bloquea.

**Aprendizaje federado (federated learning)**
Entrenar en varios clientes sin compartir datos. DiLoCo es una variante con
muchos pasos locales y outer optimizer.

**Asíncrono (async)**
Los nodos no esperan a los demás en cada ronda. Más tolerante a stragglers, más
difícil de razonar y reproducir. No es v1.

## B

**Base (base model)**
Modelo preentrenado que se congela y se adapta. En este proyecto, 32B/70B en
NF4 o bf16.

**Batch global (global batch)**
Tokens o secuencias que procesa el run por paso, sumando todas las islas:
`M · micro_batch · grad_accum`.

**bf16**
Float de 16 bits con rango amplio. Preferido en Ampere+ para entrenamiento.

**BitsAndBytes (bnb)**
Librería de cuantización y optimizadores; implementa NF4 para QLoRA.

## C

**Checkpoint**
Estado guardado para reanudar: adaptador, outer state, ronda, hashes y sampler.

**Churn**
Nodos que entran y salen con frecuencia. Degrada la utilización y la
reproducibilidad.

**Compute utilization (CU)**
Fracción del tiempo de GPU que se pasa entrenando, no esperando o sincronizando.

**Coordinador (coordinator)**
Servicio que gestiona membresía, rondas, agregación y checkpoints.

**Cosine similarity**
Similitud entre dos vectores. Se usa para detectar un delta que apunta en
dirección opuesta a la media (posible nodo malo).

## D

**DDP (Distributed Data Parallel)**
Data parallelism de PyTorch, con all-reduce de gradientes en cada paso. Sirve
dentro de una isla; no a través de WAN.

**Delta (Δ)**
Diferencia entre el modelo global y el local después de `H` pasos:
`Δ_i = θ_global - θ_local`. Es el pseudo-gradiente.

**DiLoCo**
Distributed Low-Communication: muchos pasos locales, sincronización
infrecuente, outer Nesterov.

**DoRA**
Weight-Decomposed Low-Rank Adaptation; descompone magnitud y dirección. Añade
parámetros y cómputo.

**DP (data parallelism)**
Cada GPU tiene una copia del modelo y sincroniza gradientes cada paso. Requiere
red rápida.

**Deadline**
Hora límite de una ronda; los deltas que llegan después se rechazan.

## E

**Error feedback (EF)**
Acumular el error de compresión (por ejemplo, top-k) y sumarlo al siguiente
delta. Necesario para sparsity agresiva.

**Estrella (star)**
Topología con un coordinador central. Simple, pero el coordinador es cuello de
botella.

**Época (epoch)**
Una pasada completa por el dataset.

## F

**FSDP**
Fully Sharded Data Parallel; reparte pesos, gradientes y estados entre GPUs.
FSDP2 usa `DTensor` y `fully_shard`.

**Firma (signature)**
Prueba criptográfica de que un update lo envió un nodo. En v1, Ed25519.

## G

**Goodput**
Tokens que contribuyen a rondas confirmadas por segundo. Penaliza el trabajo
desperdiciado.

**Grad accumulation**
Acumular gradientes de varios micro-batches antes de un paso de optimizer.

**Gradient checkpointing**
Recalcular activaciones en el backward para ahorrar VRAM a cambio de cómputo.

## H

**H**
Número de pasos locales entre sincronizaciones. También llamado *local steps* o
*inner steps*.

**Heartbeat**
Señal periódica de un nodo para indicar que está vivo.

**Hivemind**
Librería P2P usada por OpenDiLoCo para all-reduce entre nodos.

## I

**Inner optimizer**
Optimizador local (AdamW) que entrena dentro de la isla.

**Int8**
Cuantización a 8 bits; 2× menos bytes que bf16.

**Isla (island / worker)**
Unidad de sincronización DiLoCo. Puede ser una GPU o un nodo multi-GPU.

## K

**KV cache**
Caché de claves/valores en inferencia; no se usa en entrenamiento (se desactiva).

## L

**Latencia**
Retardo de ida y vuelta. DiLoCo la amortiza; no es el cuello principal.

**Lease**
Concesión temporal del coordinador para evitar dos escritores.

**LoRA**
Low-Rank Adaptation; adaptador de bajo rango sobre una base congelada.

**Loss gap**
Diferencia relativa de pérdida entre DiLoCo y el baseline.

## M

**M**
Número de islas DiLoCo.

**MFU**
Model FLOPs Utilization; fracción de los FLOPs pico que se aprovechan.

**Momento (momentum)**
Media móvil de gradientes/deltas. En DiLoCo, momento Nesterov exterior.

**MoE**
Mixture of Experts; solo algunos expertos se activan por token. Más memoria
total, menos cómputo activo.

## N

**NAT traversal**
Técnica para conectar nodos detrás de NAT sin abrir puertos. Tailscale/STUN.

**Nesterov**
Variante de momento que evalúa el gradiente en la posición “adelantada”.

**NF4**
NormalFloat4; cuantización de 4 bits usada por QLoRA. Base de 70B ≈ 35-40 GB.

**Non-IID**
Datos de cada nodo con distribuciones distintas. DiLoCo es robusto, pero hay
que medirlo.

## O

**Outer optimizer**
Optimizador que combina los pseudo-gradientes y actualiza el modelo global
(Nesterov en DiLoCo).

**Outer LR (η)**
Learning rate del outer optimizer. 0.7 es el valor de referencia; barrer.

## P

**PEFT**
Parameter-Efficient Fine-Tuning; librería de Hugging Face para LoRA/DoRA.

**Pseudo-gradiente**
`θ_global - θ_local` después de `H` pasos. No es un gradiente instantáneo; es
el desplazamiento acumulado.

**Permissionless**
Cualquiera puede unirse sin allowlist. No es v1; requiere verificación y
robustez.

## Q

**QLoRA**
LoRA sobre una base cuantizada a 4 bits (NF4), con doble cuantización.

**Quórum**
Número mínimo de updates válidos para confirmar una ronda.

## R

**Ronda (round)**
Ciclo de `H` pasos locales + sincronización + outer step.

**rsLoRA**
Variante de LoRA con escalado por rango.

## S

**Safetensors**
Formato de serialización de tensores sin ejecución de código.

**Shard**
Partición de datos o de modelo. En datos, cada isla ve un shard; en FSDP, cada
GPU posee un shard de parámetros.

**SparseLoCo**
DiLoCo con top-k + 2 bits + error feedback; para full fine-tuning/preentrenamiento.

**Straggler**
Nodo más lento que retrasa la ronda.

**Sync fraction**
Tiempo de sincronización / tiempo total de ronda.

## T

**Thundering herd**
Muchos nodos reintentando a la vez y saturando el coordinador. Mitigar con
jitter y backoff.

**Top-k**
Quedarse con los `k` valores de mayor magnitud; el resto se envía como cero.

**TP / tensor parallelism**
Repartir una capa entre GPUs. No es v1; requiere interconexión rápida.

## U

**Uptime / churn**
Tiempo que un nodo está disponible; el churn es la tasa de entradas/salidas.

## V

**VRAM**
Memoria de la GPU. El recurso que decide si 32B/70B caben.

## W

**WAN**
Wide Area Network; internet o enlaces entre sitios. Latencia y ancho de banda
variables.

**Warmup**
Aumento gradual del learning rate al inicio.

**Weight decay**
Regularización L2 desacoplada en AdamW.

## Acrónimos

| Acrónimo | Significado |
|---|---|
| AdamW | Adam with decoupled Weight decay |
| CU | Compute Utilization |
| DDP | Distributed Data Parallel |
| DiLoCo | Distributed Low-Communication |
| DoRA | Weight-Decomposed Low-Rank Adaptation |
| DP | Data Parallel |
| EF | Error Feedback |
| FSDP | Fully Sharded Data Parallel |
| LoRA | Low-Rank Adaptation |
| MFU | Model FLOPs Utilization |
| MoE | Mixture of Experts |
| NF4 | NormalFloat4 |
| PEFT | Parameter-Efficient Fine-Tuning |
| QLoRA | Quantized LoRA |
| WAN | Wide Area Network |
| ZeRO | Zero Redundancy Optimizer |
