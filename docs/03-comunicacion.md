# 03. Comunicación, compresión y presupuesto de red

Este documento define qué se envía, cómo se serializa, cómo se comprime y
cuánto tarda en redes domésticas reales. La regla es simple: **primero medir,
después comprimir**. Un adaptador de 70B ya es pequeño; no conviene añadir
complejidad de cuantización agresiva antes de saber si el cuello de botella es
la subida, el coordinador o el cómputo.

## 1. Qué cruza la red

| Dato | Frecuencia | Tamaño típico | ¿Se comprime? |
|---|---|---:|---|
| Config y manifiesto de ronda | Cada ronda | KB | No |
| Delta del adaptador | Cada ronda | 30 MB-1 GB | Sí (int8/bf16) |
| Adaptador global | Cada ronda | Igual que el delta | Sí |
| Checkpoint de adaptador | Cada `N` rondas | Igual que el delta | Ya comprimido |
| Checkpoint full | Raro | 35-140 GB | Sharded |
| Heartbeat | 5-10 s | Bytes | No |
| Métricas | Cada ronda | KB | No |
| Base del modelo | Una vez por nodo | 17-40 GB NF4 | No (ya cuantizada) |
| Shard de datos | Una vez o por ronda | MB-GB | Depende |

Lo que **no** se envía en v1: la base en cada ronda, el estado completo de
AdamW, el dataset completo al coordinador, ni `pickle`.

## 2. Mensajes del protocolo

Una API mínima, independiente del transporte:

```text
JoinRequest
  run_id, node_id, base_hash, config_hash, code_commit,
  gpu_name, vram_gb, bandwidth_mbps, capabilities

JoinResponse
  accepted, role, global_adapter_hash, round, lease_expires_at

Heartbeat
  run_id, node_id, round, status, gpu_util, vram_used, free_disk_gb

RoundStart
  run_id, round, global_adapter_hash, base_hash,
  config_hash, deadline_unix, nonce, shard_id

Update
  run_id, round, node_id, base_hash, adapter_hash,
  dtype, shape, compression, uncompressed_bytes,
  compressed_bytes, checksum_sha256, norm, payload

RoundResult
  run_id, round, status, global_adapter_hash,
  accepted_nodes, rejected_nodes, metrics, next_deadline_unix

Error
  code, message, retryable, round, node_id
```

Reglas:

- `run_id + round + node_id` es la clave de idempotencia.
- `nonce` y `round` evitan replay.
- `base_hash` y `config_hash` deben coincidir con el run; si no, `409`.
- `Update` es inmutable; un reintento con el mismo hash devuelve el resultado
  anterior.
- Los mensajes de control son JSON/Protobuf; los tensores van en
  `safetensors` como `bytes`, no embebidos en JSON.

## 3. Serialización e integridad

- **Tensores:** `safetensors`; es seguro, sin ejecución de código, y permite
  cargar por tensor.
- **Metadatos:** Protobuf si se usa gRPC; JSON para el CLI y los logs.
- **Hash:** SHA256 del payload comprimido y del tensor descomprimido.
- **Validación:** nombre, shape, dtype, `numel`, rango de valores y norma antes
  de agregar.
- **Límites:** tamaño máximo por mensaje y por tensor; rechazar payloads que
  excedan el presupuesto.
- **Nunca:** `torch.save`/`pickle` de datos que vienen de otro nodo.

## 4. Compresión

| Método | Bytes/param | Ahorro vs bf16 | Evidencia | Complejidad | Uso recomendado |
|---|---:|---:|---|---|---|
| bf16/fp16 | 2 | 1× | OpenDiLoCo: fp16 sin degradación | Baja | v1 |
| int8 simétrico | 1 | 2× | INTELLECT-1: int8 sin impacto apreciable | Baja | P2.1 (hecho) |
| 4 bits | 0.5 | 4× | Streaming DiLoCo | Media | v2 |
| Top-k 1-3% + 2 bits | ~0.0025-0.0075 + índices | 50-100× | SparseLoCo | Alta | Full fine-tuning |
| DCT + top-k (DeMo) | variable | 44-85× | DeMo | Media | Paso a paso, no DiLoCo |

### 4.1 Cuantización int8

Esquema recomendado para el delta:

```text
Para cada tensor:
    amax = max(abs(x))
    scale = amax / 127
    q = round(x / scale).clamp(-127, 127).to(int8)
    x' = q * scale
```

- Simétrica, por tensor o por bloque de 64-128 elementos.
- `amax` se guarda en `fp32`; el payload incluye `scale` y `zero_point` (0).
- Para tensores con outliers, usar escala por bloque; una sola escala global
  puede perder precisión.
- Error feedback opcional: si se cuantiza el mismo tensor en rondas
  consecutivas, acumular el residuo `e = x - x'` y sumarlo al siguiente delta.
  Para adaptadores pequeños no es imprescindible; para full fine-tuning, sí.
- Agregar siempre en `fp32`; no promediar int8 directamente.
- El adaptador global se publica en bf16/fp16 tras el outer step.

**Implementación P2.1:** escala por tensor, `scale` en `fp32`, delta int8 en
safetensors y decodificación a `fp32` antes de agregar. En Qwen2.5-0.5B el
payload pasó de 2.16 MB a 0.54 MB (4×); el mensaje HTTP completo fue 0.567 MB
(3.8×). Faltan top-k, error feedback y escala por bloque.

### 4.2 SparseLoCo (solo si se necesita)

Detalles relevantes del paper:

- Top-k por bloques: 64×64 para matrices 2D; chunks de 4096 para tensores 1D.
- Densidad 1-3%; 2 bits; cuantización estadística con lookup table.
- Error feedback con `β = 0.95`; congelar el EF durante el primer 5% de pasos
  exteriores para estabilidad.
- Reemplaza el momento exterior global por el acumulador de error feedback;
  no combinar top-k ingenuo con el Nesterov de DiLoCo.
- En 512M y 2B, supera a DiLoCo denso comunicando mucho menos.

No se adopta en v1 porque:

1. El adaptador ya es pequeño; el ahorro no compensa la complejidad.
2. La evidencia es de preentrenamiento, no de LoRA de 32B/70B.
3. El error feedback interactúa con el outer optimizer y exige barrido.

### 4.3 Compresión sin pérdida

- `zstd` sobre metadatos y JSON; no sobre tensores ya cuantizados (poco
  ahorro).
- No aplicar `gzip` a bf16 aleatorio; el ratio es bajo.
- Empaquetar varios tensores pequeños en un solo `safetensors` para reducir
  overhead de mensajes.

## 5. Presupuesto de ancho de banda

### 5.1 Fórmulas

```text
S = N · b                                  # bytes del delta por isla
t_upload = S / (BW_up · eficiencia)         # segundos
t_download = S / (BW_down · eficiencia)     # segundos
t_sync_estrella ≈ t_upload + t_download
t_sync_anillo ≈ 2S / (BW · eficiencia)      # por isla
```

Donde:

- `N` = parámetros del adaptador.
- `b` = bytes por parámetro (2 bf16, 1 int8, 0.5 4-bit).
- `BW` = ancho de banda en bytes/s (100 Mbps = 12.5 MB/s).
- `eficiencia` = 0.7-0.9 en WAN; usar 0.7 para planificar.
- `S` se multiplica por 2 si se sube y se baja el delta; si el coordinador
  publica el adaptador completo, es otro `S`.

### 5.2 Tamaños de adaptador

Estimaciones para Llama-3.1-70B y Qwen2.5-32B; la arquitectura exacta puede
cambiar ±10%.

| Modelo | Alcance de LoRA | r | Params | bf16 (MB) | int8 (MB) |
|---|---|---:|---:|---:|---:|
| 70B | q, k, v, o, gate, up, down | 8 | 103.5 M | 207 | 104 |
| 70B | q, k, v, o, gate, up, down | 16 | 207.1 M | 414 | 207 |
| 70B | q, k, v, o, gate, up, down | 32 | 414.2 M | 828 | 414 |
| 70B | solo atención | 16 | 65.5 M | 131 | 66 |
| 70B | solo q, v | 16 | 32.8 M | 66 | 33 |
| 32B | q, k, v, o, gate, up, down | 16 | 134.2 M | 268 | 134 |
| 32B | solo atención | 16 | 33.6 M | 67 | 34 |
| 32B | solo q, v | 16 | 16.8 M | 34 | 17 |

### 5.3 Tiempo de una subida

Tiempo de transferir `S` a distintas velocidades de subida. Es **ida**, no
ronda completa; la ronda suele ser ~2× si hay que bajar la nueva versión.

| Caso | bf16 | 20 Mbps | 50 Mbps | 100 Mbps | 250 Mbps | 500 Mbps | 1 Gbps |
|---|---:|---:|---:|---:|---:|---:|---:|
| 70B, todas, r=16 | 414 MB | 2m46s | 1m06s | 33s | 13s | 7s | 3s |
| 70B, atención, r=16 | 131 MB | 52s | 21s | 11s | 4s | 2s | 1s |
| 70B, q/v, r=16 | 66 MB | 26s | 11s | 5s | 2s | 1s | <1s |
| 32B, todas, r=16 | 268 MB | 1m47s | 43s | 22s | 9s | 4s | 2s |
| 32B, atención, r=16 | 67 MB | 27s | 11s | 5s | 2s | 1s | <1s |
| 32B, q/v, r=16 | 34 MB | 13s | 5s | 3s | 1s | <1s | <1s |

Con int8, dividir por 2. Con una eficiencia real del 70%, multiplicar por ~1.4.
Con `H` grande, este tiempo se amortiza; con `H` pequeño, domina la ronda.

### 5.4 Full fine-tuning

| Modelo | Delta bf16 | 50 Mbps | 100 Mbps | 500 Mbps | 1 Gbps |
|---|---:|---:|---:|---:|---:|
| 32B | 64 GB | 2h50m | 1h25m | 17m | 8.5m |
| 70B | 140 GB | 6h13m | 3h07m | 37m | 19m |

Estos tiempos son solo de una dirección. Un all-reduce en anillo mueve ~2× el
delta por isla; un coordinador en estrella mueve `M×` por la red del
coordinador. Por eso full fine-tuning de 32B/70B no es viable en fibra
doméstica: no es un problema de latencia, es de volumen.

### 5.5 Ejemplo de ronda con estrella

Supongamos:

- 70B, LoRA r=16 en todas las lineales, bf16: `S = 414 MB`.
- 4 islas, subida 100 Mbps, eficiencia 0.8.
- `H = 200`, `t_step = 1.5 s` → cómputo = 300 s.
- Estrella: cada isla sube 414 MB (33 s a 12.5 MB/s; 41 s con eficiencia 0.8);
  el coordinador recibe 1.66 GB y publica 1.66 GB. En el mejor caso, con
  paralelismo y full duplex, ~2-4 min de sincronización.
- Fracción de sync: 2-4 min / (5 min + 2-4 min) ≈ 30-45%. Demasiado.
- Opciones: `H=500` (12.5 min de cómputo) → 14-24%; int8 → 7-12%; anillo →
  ~1 min → 17%; o reducir el adaptador a atención/q-v.

Este ejemplo muestra por qué la topología y el alcance del adaptador importan
más que la latencia.

## 6. Transporte

### 6.1 Opciones

| Transporte | Ventajas | Desventajas | Uso |
|---|---|---|---|
| gRPC sobre HTTP/2 + TLS | Streaming, protobuf, deadlines, mTLS | Más dependencias; HTTP/2 en redes raras | **v1** |
| HTTP/2 con `aiohttp`/`httpx` | Simple, ubicuo | Menos tipado; streaming manual | Alternativa |
| QUIC/HTTP3 | Mejor en pérdida alta y RTT alto | Menos maduro en Python | v2 |
| SSH/rsync | Trivial para un piloto | Sin control de ronda, sin idempotencia | Solo P0 |
| `torch.distributed` (Gloo) | All-reduce probado en LAN | No tolera churn/NAT; collective rígido | Anillo v2 con cuidado |

Recomendación: gRPC con `grpcio` + `protobuf` para control y tensores; TLS/mTLS;
Tailscale/WireGuard para NAT. Si `grpcio` es un problema, empezar con HTTP/2 y
migrar.

### 6.2 Chunks y reanudación

- Chunk de 4-16 MB; el emisor calcula SHA256 por chunk y global.
- Subida reanudable: `upload_id`, `offset`, `total_size`; el receptor confirma
  cada chunk.
- Varias conexiones en paralelo (4-8) para aprovechar una sola subida; cuidado
  con la contención y el buffer bloat.
- Timeout por chunk y global; backoff exponencial con jitter.
- Si el deadline de ronda se cumple, abortar la subida y descartar el
  parcial; no mezclar chunks de dos rondas.

### 6.3 NAT y relay

- Tailscale/Netbird/WireGuard: intentar conexión directa; si falla, relay.
- El relay añade latencia y puede limitar el ancho de banda; medirlo.
- No exponer el coordinador en un puerto público sin TLS, auth y rate limit.
- En fibra doméstica, la IP puede cambiar; usar DNS dinámico o el plano de
  control de la VPN.

## 7. Monitorización de red

Métricas por isla y ronda:

- `sync_upload_bytes`, `sync_download_bytes`.
- `sync_upload_seconds`, `sync_download_seconds`.
- `sync_retries_total`, `sync_failures_total`.
- `rtt_ms_p50`, `rtt_ms_p95`, `packet_loss_pct` (si se mide).
- `chunk_retries_total`.
- `coordinator_ingress_bytes`, `coordinator_egress_bytes`.
- `round_sync_fraction`.
- `adapter_compression_ratio`.

Alertas:

- Sync > 30% de la ronda durante 3 rondas.
- Reintentos > 5% de chunks.
- Coordinador al 80% de su ancho de banda.
- RTT > 500 ms o pérdida > 2% sostenida.
- Un nodo con subida < 10 Mbps.

## 8. Seguridad de la comunicación

- TLS 1.3 y mTLS; certificados de nodo de corta duración.
- Firmar cada `Update` con la clave del nodo (Ed25519); el coordinador verifica.
- Incluir `round`, `nonce`, `run_id`, `base_hash` en la firma para evitar
  replay y cross-run.
- Validar tamaño, shape, dtype y norma antes de deserializar tensores.
- Limitar concurrencia y tasa por nodo.
- No aceptar `torch.load`, `pickle`, `eval`, `exec` ni rutas arbitrarias.
- Registrar hash de cada payload y decisión de aceptación/rechazo.

Ver [`07-seguridad.md`](07-seguridad.md) para el modelo de amenazas completo.

## 9. Hoja de cálculo para dimensionar

Variables:

```text
N  = parámetros del adaptador
b  = bytes/parámetro (2 bf16, 1 int8, 0.5 4-bit)
S  = N*b
M  = número de islas
H  = pasos locales
B  = batch local (secuencias)
L  = longitud de secuencia
t_step = segundos por paso local
BW_up, BW_down = Mbps de subida/bajada
eff = 0.7-0.9
```

Pasos:

1. Calcular `S`.
2. Calcular `t_upload = S / (BW_up·0.125·eff)` en segundos (Mbps → MB/s =
   Mbps/8).
3. Calcular `t_compute = H · t_step`.
4. Calcular `sync_fraction = t_sync / (t_compute + t_sync)`.
5. Si `sync_fraction > 0.2`, probar: int8, `H` mayor, menos parámetros de
   adaptador, anillo, o mejor subida.
6. Calcular bytes por token: `2S / (H·B·L)`.
7. Calcular tráfico por día: `rondas/día · M · 2S`.

## 10. Errores comunes

- Confundir **MB/s** con **Mbps**: 100 Mbps ≈ 12.5 MB/s, no 100 MB/s.
- Usar la velocidad de bajada contratada para planificar la subida.
- Ignorar la eficiencia TCP (70-90%) y el overhead de TLS/protobuf.
- Medir con un solo stream y concluir que la red es lenta; probar 4-8 streams.
- Comprimir bf16 aleatorio con gzip y esperar milagros.
- Enviar el adaptador completo cuando bastaba con el delta y el outer
  replicado; o al revés, enviar solo el delta y romper a los nodos que se
  reincorporan.
- No fijar deadline y dejar que un nodo lento bloquee la ronda.
- No hashear la base y aceptar un nodo con otra cuantización.
- Serializar con `pickle` “porque es más rápido”.
- Medir el tiempo de sync sin incluir la agregación en el coordinador.
