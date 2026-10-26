# 02. Arquitectura del sistema

Este documento describe los componentes, el flujo de una ronda, las topologías
soportadas y el modelo de fallos. El objetivo es que la implementación de v1 sea
lo más pequeña posible sin cerrar la puerta a escalar.

## 1. Requisitos

### Funcionales

- Entrenar adaptadores LoRA/DoRA de 32B/70B en 2-16 islas heterogéneas.
- Sincronizar solo los parámetros entrenables (adaptador), no la base.
- Reanudar un run desde el último checkpoint de ronda.
- Expulsar nodos lentos o caídos sin reiniciar el run.
- Verificar que todas las islas usan la misma base, tokenizer, config y código.
- Exponer métricas por ronda y por isla.
- Permitir un piloto en una sola GPU y escalar al mismo código.

### No funcionales

- Tolerar latencia WAN de 20-300 ms y pérdida de paquetes moderada.
- No requerir puertos públicos en las islas (NAT traversal o relay).
- No usar `pickle` en la red.
- No bloquear la GPU durante la sincronización más de lo necesario.
- Ser reproducible a nivel de ronda: mismo `config_hash`, `base_hash` y
  `data_manifest_hash`.
- Fallar cerrado ante inconsistencia de versiones.

## 2. Principios de diseño

1. **La base es inmutable.** El modelo base se carga en solo lectura y se
   verifica por hash. Nunca se sincroniza ni se modifica en v1.
2. **El adaptador es el contrato.** Solo se comunican tensores del adaptador,
   más metadatos pequeños.
3. **Rondas atómicas.** Una ronda se confirma entera o se descarta. No hay
   actualizaciones parciales.
4. **Coordinador simple, islas autónomas.** Las islas pueden seguir entrenando
   si el coordinador cae; se reconectan y retoman desde el último checkpoint.
5. **Idempotencia.** Subidas, reintentos y reconexiones no deben aplicar dos
   veces la misma actualización.
6. **Observabilidad primero.** Cada ronda deja un registro con bytes, tiempos,
   pérdida, normas y hashes.
7. **Nada de magia.** Sin DDP de PyTorch a través de WAN en v1; sin NFS;
   sin `trust_remote_code=True`.

## 3. Componentes

```text
┌──────────────────────────────────────────────────────────────────────┐
│                         PLANO DE CONTROL                             │
│                                                                      │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────────────┐  │
│  │ Coordinador  │   │ Meta DB      │   │ Object store             │  │
│  │ API + rondas │◄─►│ runs/nodes/  │◄─►│ base, adapters, ckpts,   │  │
│  │ membership   │   │ rounds       │   │ datasets, manifests      │  │
│  └──────┬───────┘   └──────────────┘   └──────────────────────────┘  │
│         │                                                           │
│  ┌──────▼───────┐   ┌──────────────┐   ┌──────────────────────────┐  │
│  │ Auth/mTLS    │   │ Métricas     │   │ CLI / dashboard          │  │
│  │ allowlist    │   │ Prometheus   │   │ status, logs, rollback   │  │
│  └──────────────┘   └──────────────┘   └──────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
                                │
                    gRPC/HTTP2 + TLS, chunks, reintentos
                                │
┌───────────────────────────────┼──────────────────────────────────────┐
│                         PLANO DE DATOS                               │
│                                                                      │
│  ┌─────────────────────┐   ┌─────────────────────┐                   │
│  │ Isla A              │   │ Isla B              │   ...             │
│  │ ┌─────────────────┐ │   │ ┌─────────────────┐ │                   │
│  │ │ Worker agent    │ │   │ │ Worker agent    │ │                   │
│  │ │ heartbeat, ckpt │ │   │ │ heartbeat, ckpt │ │                   │
│  │ ├─────────────────┤ │   │ ├─────────────────┤ │                   │
│  │ │ Training engine │ │   │ │ Training engine │ │                   │
│  │ │ base NF4 + LoRA │ │   │ │ base NF4 + LoRA │ │                   │
│  │ │ DDP/local FSDP  │ │   │ │ DDP/local FSDP  │ │                   │
│  │ ├─────────────────┤ │   │ ├─────────────────┤ │                   │
│  │ │ Data shard      │ │   │ │ Data shard      │ │                   │
│  │ └─────────────────┘ │   │ └─────────────────┘ │                   │
│  └─────────────────────┘   └─────────────────────┘                   │
└──────────────────────────────────────────────────────────────────────┘
```

### 3.1 Coordinador

Responsabilidades:

- Registro de runs y nodos; allowlist y tokens.
- Máquina de estados de ronda y *leases* con deadline.
- Recepción y validación de deltas.
- Agregación y paso exterior (o coordinación de un all-reduce).
- Manifiesto de checkpoint y publicación de la siguiente versión.
- Métricas, logs de auditoría y estado para el CLI.
- No entrena en v1 (puede ser una isla en un piloto pequeño, pero no se
  recomienda).

Debe ser **idempotente**: si una isla reintenta una subida de la misma ronda,
el coordinador devuelve el resultado ya calculado y no agrega dos veces.

### 3.2 Worker agent (isla)

- Se registra, valida la base y la config, y descarga el adaptador global.
- Lanza el proceso de entrenamiento local (1 GPU o multi-GPU).
- Envía heartbeats cada 5-10 s.
- Al terminar `H` pasos, calcula el delta, lo comprime, lo firma y lo sube.
- Espera la nueva versión global, la aplica y empieza la ronda siguiente.
- Guarda checkpoints locales y limpia los temporales.
- Expone métricas de GPU, disco, red y progreso.

### 3.3 Training engine

- Carga la base cuantizada en solo lectura y el adaptador en `bf16`/`fp32`.
- Entrena `H` pasos locales con AdamW y el shard asignado.
- Si la isla tiene varias GPUs:
  - **Modo replicado:** cada GPU tiene la base y el adaptador; DDP del
    adaptador dentro de la isla. Recomendado para QLoRA.
  - **Modo sharded:** FSDP2/ZeRO-3 sobre la base y el adaptador; cada rank
    posee un shard. Necesario para full fine-tuning, más complejo.
- Calcula `Δ = θ_global - θ_local` en `fp32` (o `bf16` si se valida) y lo
  empaqueta en `safetensors`.

### 3.4 Object store

- Base pre-cuantizada o bf16, con hash.
- Adaptador global por ronda (pequeño) y checkpoints completos cada `N` rondas.
- Manifiestos de datos y de ejecución.
- Logs y artefactos de evaluación.
- Versionado y retención; nunca sobrescribir un artefacto con el mismo nombre.

### 3.5 Meta DB

Tablas mínimas:

```text
runs(id, config_hash, base_hash, data_manifest_hash, status, created_at)
nodes(id, run_id, hostname, gpu, status, last_heartbeat, bandwidth_mbps)
rounds(run_id, round, status, started_at, committed_at, global_hash, metrics)
updates(run_id, round, node_id, delta_hash, norm, bytes, received_at)
checkpoints(run_id, round, path, hash, size, created_at)
```

SQLite es suficiente para v1; PostgreSQL para concurrencia y HA.

### 3.6 Evaluador

- Servicio separado o una isla dedicada.
- Carga el adaptador global y evalúa en un holdout fijo.
- Nunca usa datos de entrenamiento.
- Puede ejecutarse en paralelo con la siguiente ronda si hay GPU libre; si no,
  se pausa el run o se evalúa cada `N` rondas.

## 4. Topologías

### 4.1 Estrella con coordinador (v1)

```text
        Isla A ──┐
        Isla B ──┼──► Coordinador ──► Isla A/B/C
        Isla C ──┘
```

- **Ventajas:** simple, NAT-friendly, fácil de auditar, no requiere un
  colectivo distribuido, el coordinador puede ser el único punto con IP
  pública/relay.
- **Desventajas:** el coordinador es cuello de botella de ancho de banda y de
  disponibilidad. Para `M` islas, entrada `M·S` y salida `M·S` por ronda.
- **Uso:** 2-4 islas con adaptadores de hasta ~100-200 MB; o más islas si el
  adaptador es pequeño (q/v) y la subida es buena.
- **No usar:** 8 islas × 70B LoRA completo con subida de 50 Mbps; el
  coordinador tardará más que el cómputo.

### 4.2 Anillo / all-reduce (v2)

```text
        Isla A ──► Isla B
          ▲          │
          │          ▼
        Isla D ◄── Isla C
```

- **Ventajas:** cada isla mueve ~2·S, independiente de `M`; no hay coordinador
  de datos.
- **Desventajas:** un nodo lento o caído bloquea el anillo; NAT traversal más
  difícil; hace falta un colectivo tolerante a fallos (tipo `ElasticDeviceMesh`
  de `prime`) o reconstruir el grupo cada ronda.
- **Uso:** 4-16 islas con deltas grandes; redes estables; se puede implementar
  con `torch.distributed` + Gloo sobre VPN, o con gRPC en anillo.
- **Requisito:** un plano de control separado para membresía y reintentos.

### 4.3 Árbol jerárquico (v3)

```text
                 Coordinador
                /           \
          Agregador 1     Agregador 2
          /    |    \      /    |    \
        I1    I2    I3   I4    I5    I6
```

- **Ventajas:** reduce la carga del coordinador; tolera mejor la latencia.
- **Desventajas:** más piezas; la agregación parcial debe ser exacta y
  verificable; los agregadores son nuevos puntos de fallo.
- **Uso:** 16+ islas o redes con regiones.

### 4.4 Gossip / aproximado (investigación)

- Mezcla no bloqueante entre vecinos; no hay ronda global exacta.
- Más robusto a fallos y stragglers, pero la convergencia y la reproducibilidad
  son más difíciles de garantizar.
- Se evalúa solo si v2 falla por churn.

### 4.5 Tabla comparativa

| Topología | Bytes por isla | Cuello de botella | Tolerancia a fallos | Complejidad | Uso |
|---|---:|---|---|---|---|
| Estrella | ~2S | Coordinador | Media (reintentos) | Baja | v1, 2-4 islas |
| Anillo | ~2S | Nodo más lento | Baja sin elasticidad | Media | v2, 4-16 islas |
| Árbol | ~2S + overhead | Agregadores | Media | Alta | v3, 16+ |
| Gossip | variable | Convergencia | Alta | Alta | Investigación |

`S` = bytes del delta por isla y ronda.

## 5. Máquina de estados de una ronda

```text
IDLE
  │ RoundStart
  ▼
ASSIGNED
  │ workers confirm
  ▼
TRAINING (H pasos locales)
  │ upload
  ▼
UPLOADING
  │ all updates or deadline
  ▼
AGGREGATING
  │ outer step + checkpoint
  ▼
COMMITTED ──► IDLE (siguiente ronda)
  │
  ├── TIMEOUT ──► ABORTED (se descarta la ronda; los que llegaron tarde
  │                        reintentan en la siguiente)
  └── ERROR   ──► PAUSED (intervención manual)
```

Reglas:

- Una ronda solo se confirma si hay al menos `quorum` actualizaciones válidas.
  `quorum = max(2, ceil(0.6 · M_expected))`; configurable.
- Las actualizaciones que llegan después del deadline se rechazan con
  `ROUND_CLOSED`; la isla debe descargar la versión global y unirse a la
  siguiente ronda.
- El checkpoint se escribe antes de publicar `COMMITTED`; si falla el
  checkpoint, no se publica la ronda.
- `run_id + round + node_id` es la clave de idempotencia.

## 6. Secuencia de una ronda (estrella)

```text
Coordinador                 Isla A                  Isla B
     │                        │                       │
     │  RoundStart(θ, hash)   │                       │
     ├───────────────────────►│                       │
     ├───────────────────────────────────────────────►│
     │                        │ H pasos locales       │
     │                        │ Δ_A                   │
     │  Update(Δ_A, hash)     │                       │
     │◄───────────────────────┤                       │
     │                        │                       │ Δ_B
     │  Update(Δ_B, hash)     │                       │
     │◄───────────────────────────────────────────────┤
     │  validar + agregar + outer + checkpoint        │
     │  RoundResult(θ', hash) │                       │
     ├───────────────────────►│                       │
     ├───────────────────────────────────────────────►│
     │                        │ siguiente ronda       │
```

Si una isla no responde al deadline, el coordinador la marca `LATE` y continúa
con las que sí respondieron, siempre que se alcance el quórum.

## 7. Modos de isla

### 7.1 Replicado (recomendado para QLoRA)

- Cada GPU de la isla tiene la base cuantizada completa y una copia del
  adaptador.
- DDP del adaptador dentro de la isla: gradientes locales cada paso, all-reduce
  por NVLink/PCIe; luego un solo delta DiLoCo por isla.
- **Requisito:** la base debe caber en cada GPU. 32B en 24 GB es ajustado; 70B
  requiere 48 GB+.
- **Ventaja:** simple, no hay que shardear la base, el adaptador es pequeño.
- **Desventaja:** no escala a modelos que no caben en una GPU.

### 7.2 Sharded (full fine-tuning o base que no cabe)

- FSDP2/ZeRO-3 reparte pesos, gradientes y estados entre las GPUs de la isla.
- El pseudo-gradiente se calcula por shard; la agregación DiLoCo se hace por
  shard (cada rango sincroniza su parte).
- El optimizador exterior puede vivir en CPU y estar replicado o shardeado.
- **Ventaja:** permite 32B/70B full fine-tuning con suficientes GPUs.
- **Desventaja:** complejidad; interactúa con PEFT/QLoRA; no es v1.

### 7.3 Pipeline (100B+)

- Pipeline parallelism dentro de la isla; DiLoCoX es la referencia.
- No es una ruta doméstica; se documenta como trabajo futuro.

## 8. Distribución de la base y los datos

### 8.1 Base

1. Se elige una revisión exacta del modelo (commit de Hugging Face).
2. Se pre-cuantiza a NF4 con bitsandbytes en una máquina de confianza y se
   publica en el object store con SHA256.
3. Cada isla descarga una vez, verifica el hash y la mantiene en caché.
4. Si se usa bf16 en vez de NF4, el hash también se verifica.
5. La versión de bitsandbytes y la config de cuantización se registran en el
   manifiesto; una diferencia invalida la isla.

No se cuantiza en cada isla al vuelo salvo que se acepte el riesgo de
diferencias numéricas. Preferir una base pre-cuantizada y distribuida.

### 8.2 Datos

- El coordinador publica un **manifiesto de datos** con shards, hashes y
  rangos.
- Cada isla descarga solo su shard (o lo tiene local) y verifica el hash.
- El shard se baraja con una semilla derivada de `run_id + round`; así todos
  los nodos cubren el dataset de forma reproducible sin compartir datos.
- Para datos privados, el coordinador solo distribuye hashes/índices, no el
  contenido; cada isla aporta su propio corpus. Esto requiere confianza en el
  nodo.

## 9. Tolerancia a fallos

| Fallo | Detección | Acción |
|---|---|---|
| Isla caída | Heartbeat 3× intervalo | Rejoin con el mismo `node_id`; expulsión automática pendiente |
| Isla lenta | Deadline de ronda | Marcar `LATE`; excluir de la ronda |
| Upload corrupto | SHA256 + shape/dtype | Rechazar; pedir reintento |
| Coordinador caído | health check | Reiniciar; las islas esperan y reintentan |
| Checkpoint corrupto | hash + carga de prueba | Rollback al anterior |
| Base distinta | hash de base/config | Rechazar join |
| Disco lleno | umbral 90% | Pausar; limpiar checkpoints viejos |
| Pérdida de red | timeout + backoff | Reintentar; si persiste, expulsar |
| Split-brain | lease/epoch | Solo el coordinador con lease válido escribe |

### 9.1 Join en caliente

Implementación P2.2:

1. La isla descarga el adaptador global y el manifiesto.
2. Carga la base y verifica hashes.
3. Reinicia el estado de AdamW local (o lo carga de un checkpoint si existe).
4. Se registra. Si `coordinator.allow_late_join` es `true`, puede entrar con
   una ronda en curso; recibe el estado actual y entrena `H` pasos.
5. Si la ronda avanza mientras entrena, la subida devuelve `409 stale round`;
   el worker recarga el estado y reintenta hasta 3 veces.

Sigue siendo recomendable entrar al inicio de ronda para no competir con el
deadline. Un `node_id` que ya estaba registrado puede reconectarse sin depender
de `allow_late_join`. La expulsión automática por heartbeat y el `Leave`
ordenado siguen pendientes; si no hay quórum, el run falla en vez de degradar.
`prime` va más allá: la isla se une al outer step con pseudo-gradiente cero y
salta los pasos interiores; es una optimización para runs largos, no necesaria
en v1.

### 9.2 Salida ordenada

Pendiente en P2.2:

- Añadir `Leave` para que el coordinador elimine la isla antes del siguiente
  `RoundStart`.
- Si está a mitad de ronda, descartar su delta; no mezclar un delta parcial.
- Guardar su estado local por si vuelve.

Hoy una isla que termina deja de mandar heartbeat; el coordinador no la expulsa
automáticamente y el deadline de ronda sigue siendo la única barrera.

### 9.3 Reanudación

- El checkpoint global contiene: adaptador, estado del outer optimizer, número
  de ronda, hashes, config y estado del sampler.
- Las islas descargan el checkpoint global y reinician su optimizer interior.
- No se reutiliza un delta de una ronda abortada.
- La reanudación es a nivel de ronda, no de paso.

## 10. Capacidad y límites

### 10.1 Coordinador

- Ancho de banda de entrada/salida: `M · S` por ronda.
- CPU: agregación en `fp32` de `N` parámetros; para 207M es trivial; para 70B
  full fine-tuning, 140 GB por ronda y una CPU potente.
- Almacenamiento: adaptadores (pequeños) + checkpoints (grandes).
- Si el coordinador también entrena, su GPU y su red compiten; evitar.

### 10.2 Object store

- Base 70B NF4: ~35-40 GB.
- Base 32B NF4: ~17-20 GB.
- Adaptador 70B r=16 all-linear: ~0.4 GB; r=32: ~0.8 GB.
- Checkpoint de adaptador por ronda: duplicar el tamaño del adaptador.
- Checkpoint full 70B: ~140 GB (bf16) o sharded; retención corta.

### 10.3 Límites prácticos de v1

- 2-4 islas si el adaptador es grande (>200 MB) y la subida es <100 Mbps.
- Hasta 8 islas si el adaptador es pequeño (<100 MB) o la subida es ≥250 Mbps.
- 32B antes que 70B; 0.5B/1.5B antes que 32B.
- No se soporta churn agresivo ni nodos permissionless en v1.

## 11. Decisiones de arquitectura

| ID | Decisión | Alternativa descartada | Motivo |
|---|---|---|---|
| A-01 | Adaptador como unidad de sincronización | Modelo completo | 70B full no cabe en nodos domésticos |
| A-02 | Estrella para v1 | Anillo desde el día 1 | Menos piezas; anillo requiere colectivo tolerante a fallos |
| A-03 | Coordinador central para outer step | Outer replicado en cada isla | Evita drift numérico y simplifica la auditoría |
| A-04 | Rondas síncronas con deadline | Async puro | Reproducible y fácil de depurar |
| A-05 | Base NF4 pre-cuantizada | Cuantizar en cada isla | Consistencia numérica y hash verificable |
| A-06 | gRPC/HTTP2 + TLS | SSH/rsync | Streaming, reintentos, auth y métricas |
| A-07 | SQLite + filesystem en v1 | PostgreSQL + S3 desde el día 1 | Menos infraestructura; migrar cuando haga falta |
| A-08 | No DDP de PyTorch sobre WAN | `torch.distributed` global | Los colectivos no toleran churn ni NAT |

## 12. Riesgos de arquitectura

| Riesgo | Consecuencia | Mitigación |
|---|---|---|
| Coordinador único | Punto de fallo y cuello de botella | Checkpoints, reinicio, réplica caliente en v2 |
| Estrella con M grande | Ronda dominada por red | Anillo/árbol, int8, reducir alcance del adaptador |
| Relojes desincronizados | Deadlines y logs confusos | NTP; usar timestamps del coordinador |
| Base pre-cuantizada distinta | Deltas inválidos | Hash y versión de bitsandbytes |
| Checkpoint durante ronda | Estado inconsistente | Solo en frontera de ronda; escritura atómica |
| Isla con datos sesgados | Adaptador peor | Shards estratificados, evaluación, mezcla |
| Nodo malicioso | Envenenamiento | Allowlist v1; controles en `07-seguridad.md` |

## 13. Futuro

- **v2:** anillo/árbol con membresía elástica; int8; reintentos resumibles.
- **v3:** solapamiento de comunicación con cómputo (Streaming DiLoCo);
  fragmentación del adaptador.
- **v4:** full fine-tuning con FSDP2 y pseudo-gradientes shardeados; evaluación
  de SparseLoCo.
- **Largo plazo:** gossip tolerante a fallos para nodos permissionless, solo
  después de resolver verificación y robustez.
