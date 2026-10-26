# 06. Operaciones, despliegue y runbook

Este documento asume que el diseño de [`02-arquitectura.md`](02-arquitectura.md)
ya está implementado. Describe cómo desplegar, observar y recuperar un run.

## 1. Topología de despliegue

```text
┌─────────────────────────────────────────────────────────────┐
│ Nodo coordinador (VPS o máquina fija)                       │
│ - API gRPC/HTTP2 + TLS                                      │
│ - SQLite/PostgreSQL                                         │
│ - MinIO/S3 (o disco local versionado)                       │
│ - Prometheus + Grafana + Loki                               │
│ - Backup                                                   │
└───────────────────────┬─────────────────────────────────────┘
                        │ Tailscale/WireGuard
        ┌───────────────┼────────────────┬─────────────────┐
        │               │                │                 │
   ┌────▼────┐     ┌────▼────┐      ┌────▼────┐      ┌─────▼────┐
   │ Isla 1  │     │ Isla 2  │      │ Isla 3  │      │ Isla 4   │
   │ 48 GB   │     │ 48 GB   │      │ 24 GB   │      │ 80 GB    │
   │ 100/20  │     │ 500/100 │      │ 300/50  │      │ 1000/500 │
   └─────────┘     └─────────┘      └─────────┘      └──────────┘
```

- El coordinador no necesita GPU en v1.
- Una isla puede ser el coordinador en un piloto de 2 nodos, pero la red y la
  GPU compiten; no hacerlo en runs largos.
- El object store puede ser MinIO en el coordinador o S3 externo. Para 70B,
  prever 200-500 GB.
- Tailscale/Netbird simplifica NAT y mTLS; WireGuard puro requiere más
  configuración.

## 2. Coordinador

### 2.1 Requisitos

- 8-16 vCPU, 32-64 GB RAM (más si agrega full fine-tuning).
- 1-2 TB de disco para checkpoints y base; SSD.
- IP estable o DNS dinámico; puerto 443 saliente/entrante.
- Backups diarios de la DB y del object store.

### 2.2 Docker Compose mínimo

```yaml
services:
  coordinator:
    image: registry.example/diloco-coordinator:0.1.0
    restart: unless-stopped
    ports:
      - "443:8443"
    volumes:
      - ./data:/var/lib/diloco
      - ./certs:/etc/diloco/certs:ro
    environment:
      DILOCO_DB_URL: sqlite:////var/lib/diloco/coordinator.db
      DILOCO_OBJECT_STORE: file:///var/lib/diloco/objects
      DILOCO_TLS_CERT: /etc/diloco/certs/server.crt
      DILOCO_TLS_KEY: /etc/diloco/certs/server.key
      DILOCO_REQUIRE_MTLS: "true"

  minio:
    image: minio/minio:latest
    command: server /data --console-address ":9001"
    volumes:
      - ./minio:/data

  prometheus:
    image: prom/prometheus:latest
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro

  grafana:
    image: grafana/grafana:latest
    volumes:
      - ./grafana:/var/lib/grafana
```

En producción, fijar versiones por digest, no `latest`.

### 2.3 Arranque

```bash
# 1. Crear estructura y certificados
mkdir -p data/certs data/objects data/logs

# 2. Migrar la DB
docker compose run --rm coordinator diloco-admin migrate

# 3. Crear el run (config + hashes)
docker compose run --rm coordinator diloco-admin run create \
  --config /config/run.yaml

# 4. Arrancar
docker compose up -d coordinator
```

### 2.4 Alta disponibilidad

v1: un solo coordinador con reinicio automático y checkpoints. Si cae:

1. El proceso se reinicia.
2. Las islas reintentan con backoff.
3. Se recupera la última ronda confirmada.
4. Las islas que iban por delante descargan el estado global y se reincorporan.

v2: dos coordinadores con un lease en PostgreSQL/etcd; solo el líder escribe.
No implementar HA casero con dos escritores.

## 3. Isla / worker

### 3.1 Requisitos

- GPU con VRAM suficiente según [`04-modelos.md`](04-modelos.md).
- 64-128 GB RAM; 100-300 GB de disco libre.
- Conexión por cable; evitar Wi-Fi para entrenamiento.
- Tailscale/WireGuard activo; NTP.
- Docker + NVIDIA Container Toolkit.
- Usuario sin privilegios; el contenedor no necesita `--privileged`.

### 3.2 Systemd

```ini
[Unit]
Description=DiLoCo worker
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
Restart=always
RestartSec=10
ExecStart=/usr/bin/docker run --rm --gpus all \
  --name diloco-worker \
  -v /srv/diloco/cache:/cache \
  -v /srv/diloco/data:/data:ro \
  -v /srv/diloco/logs:/logs \
  -e DILOCO_COORDINATOR=coordinator.example:443 \
  -e DILOCO_NODE_ID=%H \
  -e DILOCO_RUN_ID=zig-32b-qlora-diloco-v1 \
  registry.example/diloco-worker:0.1.0
ExecStop=/usr/bin/docker stop diloco-worker

[Install]
WantedBy=multi-user.target
```

### 3.3 Arranque de una isla

```bash
# Verificar GPU y red
nvidia-smi
iperf3 -c coordinator.example -p 5201 -t 20

# Verificar que la base está en caché y el hash coincide
diloco-worker verify-base --config /etc/diloco/worker.yaml

# Arrancar
sudo systemctl enable --now diloco-worker
journalctl -u diloco-worker -f
```

### 3.4 Aislamiento

- Contenedor sin privilegios, con `--read-only` para el sistema de archivos y
  volúmenes explícitos.
- Sin acceso a Docker socket.
- Sin `trust_remote_code=True`.
- Datos montados en solo lectura; caché de modelos en escritura.
- Límite de CPU/RAM para evitar que el dataloader consuma todo.
- Egress limitado al coordinador y al object store.

## 4. Red

### 4.1 Tailscale/Netbird

- Crear una red privada; solo el coordinador expone el servicio.
- ACLs: las islas solo pueden hablar con el coordinador y el object store.
- Desactivar el acceso a la LAN doméstica si no es necesario.
- Relay de la VPN como fallback; medir su throughput.

### 4.2 WireGuard puro

- Cada nodo con clave pública/privada; el coordinador con `AllowedIPs`.
- `PersistentKeepalive = 25` para NAT.
- MTU 1280-1420; probar.
- Firewall: solo UDP del puerto WireGuard y TCP 443 del coordinador.

### 4.3 Pruebas

```bash
# Latencia y pérdida
mtr -rwzbc 100 coordinator.example

# Throughput con varios streams
iperf3 -c coordinator.example -p 5201 -t 30 -P 8

# Throughput de subida desde la isla
iperf3 -c coordinator.example -p 5201 -t 30 -P 8 --reverse
```

Guardar los resultados en el manifiesto del nodo. No asumir que la subida
contratada es la efectiva.

## 5. Bootstrap de un run

1. **Congelar config y datos.** Crear `run.yaml`, manifiesto de datos y hashes.
2. **Pre-cuantizar la base** (si aplica) y subirla al object store.
3. **Crear el run** en el coordinador; el estado inicial es `CREATED`.
4. **Unir islas** con `JoinRequest`; verificar base, config y código.
5. **Dry run:** modelo diminuto o `H=2`, 1 ronda, sin escribir checkpoint
   definitivo. Verificar que el delta se agrega y que la ronda se confirma.
6. **Smoke real:** `H=10`, 50-100 pasos, una isla; comparar con LoRA normal.
7. **Run real:** `H` completo, checkpoints y evaluación.
8. **Cierre:** evaluar, exportar adaptador, archivar config y métricas.

## 6. Configuración y versionado

- `run.yaml` en git; el hash se calcula y se guarda en la DB.
- Cambiar `H`, `η` o datos a mitad de run requiere **nuevo run** o una decisión
  explícita documentada. No mutar la config en caliente.
- El coordinador rechaza un `JoinRequest` si `config_hash` no coincide.
- El código del worker y del coordinador se identifica por `git commit` o
  digest de imagen.

## 7. Monitorización

### 7.1 Métricas por isla

| Métrica | Tipo | Uso |
|---|---|---|
| `diloco_gpu_util_pct` | gauge | Detecta GPU infrautilizada |
| `diloco_gpu_mem_used_bytes` | gauge | OOM, fugas |
| `diloco_train_loss` | gauge | Convergencia |
| `diloco_grad_norm` | gauge | Estabilidad |
| `diloco_pseudo_grad_norm` | gauge | Anomalías por nodo |
| `diloco_step_time_seconds` | histogram | Throughput |
| `diloco_tokens_per_second` | gauge | Goodput local |
| `diloco_round_train_seconds` | histogram | Compute |
| `diloco_round_sync_seconds` | histogram | Red |
| `diloco_upload_bytes` | counter | Tráfico |
| `diloco_download_bytes` | counter | Tráfico |
| `diloco_retries_total` | counter | Red inestable |
| `diloco_disk_free_bytes` | gauge | Checkpoints |
| `diloco_heartbeat_age_seconds` | gauge | Nodo vivo |

### 7.2 Métricas del coordinador

| Métrica | Uso |
|---|---|
| `diloco_round_state{state}` | Estado de la ronda |
| `diloco_round_duration_seconds` | Wall-clock |
| `diloco_accepted_updates` | Quórum |
| `diloco_rejected_updates{reason}` | Integridad |
| `diloco_coordinator_ingress_bytes` | Saturación |
| `diloco_coordinator_egress_bytes` | Saturación |
| `diloco_checkpoint_duration_seconds` | I/O |
| `diloco_eval_loss` | Calidad |
| `diloco_outer_lr`, `diloco_outer_momentum` | Config efectiva |

### 7.3 Logs

- JSON estructurado, una línea por evento.
- Campos: `ts`, `level`, `run_id`, `round`, `node_id`, `event`, `duration_ms`,
  `bytes`, `hash`, `error`.
- No loggear tensores ni datos personales.
- Retención: 30 días en caliente, 1 año en archivo.
- Trace ID por ronda para correlacionar coordinador e islas.

### 7.4 Alertas

| Alerta | Condición | Acción |
|---|---|---|
| Nodo caído | heartbeat > 30 s | Expulsar; avisar |
| Ronda estancada | estado > 2× deadline | Revisar logs; abortar |
| Sync alto | > 30% durante 3 rondas | Subir `H`/int8/anillo |
| Pérdida sube | > 2× baseline durante 5 rondas | Rollback; revisar |
| Norma anómala | > 5× mediana | Clip; auditar nodo |
| Disco | < 10% | Limpiar; pausar |
| Coordinador | CPU/RAM/red > 80% | Escalar; reducir `M` |
| Error de hash | cualquiera | Fallar cerrado; investigar |
| Certificado | < 7 días | Rotar |

## 8. Runbook

### 8.1 Iniciar un run

```bash
diloco-admin run create --config configs/32b.yaml
diloco-admin run start --run-id zig-32b-qlora-diloco-v1
diloco-admin status --watch
```

### 8.2 Unir una isla

```bash
diloco-worker join \
  --coordinator coordinator.example:443 \
  --run-id zig-32b-qlora-diloco-v1 \
  --node-id isla-5 \
  --cache /srv/diloco/cache
```

La isla descarga el adaptador global, verifica la base y espera al siguiente
`RoundStart`.

### 8.3 Expulsar una isla

```bash
diloco-admin node evict --run-id ... --node-id isla-3 --reason slow
```

El coordinador deja de esperar su delta. La isla puede volver a unirse al final
de la ronda; si vuelve a fallar, se bloquea.

### 8.4 Reemplazar una isla

1. Expulsar la vieja.
2. Provisionar la nueva con la misma base/config.
3. Unirla; descarga el adaptador global.
4. Verificar que su primer delta tiene una norma razonable.
5. Si es un nodo de confianza, mantener; si no, auditar.

### 8.5 Pausar y reanudar

```bash
diloco-admin run pause --run-id ...
# esperar a que la ronda actual termine; no matar a mitad
diloco-admin run resume --run-id ...
```

Las islas reciben `PAUSED`; no empiezan una ronda nueva. El coordinador guarda
checkpoint antes de pausar.

### 8.6 Rollback

```bash
diloco-admin checkpoint list --run-id ...
diloco-admin run rollback --run-id ... --round 120
```

Requisitos: checkpoint íntegro, outer state, config compatible. Las islas
descargan la versión antigua; sus AdamW locales se reinician. Documentar el
motivo.

### 8.7 Cambiar `H` o `η`

No en caliente. Opciones:

- Terminar el run y crear uno nuevo desde el checkpoint con otra config.
- Si se permite, crear un “run hijo” con `parent_run_id` y `start_round`.

El coordinador debe rechazar un `JoinRequest` con un `config_hash` distinto.

### 8.8 Exportar el adaptador

```bash
diloco-admin export adapter \
  --run-id ... --round best \
  --out /artifacts/zig-32b-lora.safetensors
```

Verificar hash, cargarlo con PEFT y evaluar. Para servir, usar el adaptador
sobre la base original (no NF4 si el servidor no lo soporta) o fusionar en una
máquina con RAM suficiente.

### 8.9 Abortar

```bash
diloco-admin run abort --run-id ... --reason divergence
```

Guardar el último checkpoint válido, los logs y las métricas. No borrar el run
hasta el postmortem.

### 8.10 Incidente de seguridad

1. Aislar el nodo (expulsar, revocar certificado).
2. Pausar el run.
3. Revisar logs y deltas del nodo.
4. Rollback al checkpoint anterior al primer delta sospechoso.
5. Rotar claves y tokens.
6. Documentar y notificar si hay datos personales.

Ver [`07-seguridad.md`](07-seguridad.md).

## 9. Capacidad y costes

### 9.1 Almacenamiento

| Artefacto | 32B | 70B |
|---|---:|---:|
| Base NF4 | 17-20 GB | 35-40 GB |
| Base bf16 (para fusionar/servir) | 64 GB | 140 GB |
| Adaptador r=16 todas | 0.27 GB | 0.41 GB |
| Checkpoint de adaptador (con outer) | ~0.5 GB | ~0.8 GB |
| Dataset (100 M tokens, texto) | ~0.2-0.5 GB | ~0.2-0.5 GB |
| Logs/métricas | < 1 GB | < 1 GB |

Para un run de 500 rondas con checkpoint cada ronda y retención de 5:
`5 × 0.8 GB = 4 GB` en 70B, más la base. Manejable.

### 9.2 Tráfico

```text
tráfico/día ≈ rondas/día · M · 2S · (1 + overhead)
```

Ejemplo 70B r=16 todas, `S=414 MB`, 4 islas, 20 rondas/día:

```text
4 · 2 · 414 MB · 20 ≈ 66 GB/día  (sin contar reintentos)
```

Con int8, ~33 GB/día. Revisar los límites del ISP y el coste de egress del
object store.

### 9.3 Electricidad

```text
kWh/día ≈ potencia_media_kW · 24 · nodos
coste/día ≈ kWh/día · precio_kWh
```

Un nodo de 0.5 kW durante 24 h consume 12 kWh/día. A 0.15 €/kWh, 1.8 €/nodo/día.
La red y el coordinador añaden poco; el coste real es el hardware y el tiempo.

### 9.4 Hardware

No comprar 70B para este experimento sin pasar F1-F3. El coste de una GPU de
48 GB y de la infraestructura de 8 nodos supera con creces el de un piloto de
0.5B/7B que valide la receta. Alquilar por horas para F3/F4 puede ser más
racional que comprar.

## 10. Backup y recuperación

- DB: backup diario + WAL; probar restauración.
- Object store: versionado; no sobrescribir checkpoints.
- Config y código: git; tag por run.
- Certificados y claves: gestor de secretos; no en el repo.
- Un backup que no se ha restaurado no es un backup.

## 11. Mantenimiento

- Actualizar drivers/CUDA solo entre runs; fijar versiones en la imagen.
- Rotar certificados antes de que expiren.
- Revisar discos y logs semanalmente.
- Probar el runbook de caída de coordinador y de nodo una vez por trimestre.
- Revisar costes y tráfico.
- Auditar nodos y permisos.

## 12. Checklists

### 12.1 Antes de un run

- [ ] Base y tokenizer con revisión y hash.
- [ ] Config congelada y hasheada.
- [ ] Datos sin leakage; holdout separado.
- [ ] VRAM estimada y probada con `H=2`.
- [ ] Subida medida con `iperf3`.
- [ ] Deadline y quórum definidos.
- [ ] Checkpoint y rollback probados.
- [ ] Alertas y dashboard activos.
- [ ] Certificados y ACLs revisados.
- [ ] Backup de la DB.

### 12.2 Después de un run

- [ ] Adaptador exportado y hasheado.
- [ ] Evaluación completa y pareada con baseline.
- [ ] Métricas de sync, CU, goodput y stragglers.
- [ ] Config, versiones y logs archivados.
- [ ] Coste y tráfico calculados.
- [ ] Incidentes y decisiones documentados.
- [ ] Checkpoints antiguos limpiados según retención.

## 13. Anti-patrones operativos

- Coordinador en una laptop doméstica con Wi-Fi y suspensión.
- Entrenar en Wi-Fi “porque funciona para SSH”.
- Guardar checkpoints en NFS sobre WAN.
- Reiniciar el coordinador a mitad de ronda sin checkpoint.
- Cambiar la config y reutilizar el mismo `run_id`.
- Aceptar nodos sin verificar la base.
- Ignorar el reloj; deadlines y logs inconsistentes.
- Medir throughput con el evaluador corriendo en la misma GPU.
- No limitar el dataloader; el sistema operativo hace swap.
- Usar `latest` en las imágenes.
- Compartir claves privadas entre nodos.
- Borrar logs “porque ocupan espacio”.
