# Validación WAN / NAT multi-máquina (P2.4)

Runbook para validar el entrenamiento DiLoCo sobre internet real con los peers
detrás de NAT. **Requiere 2+ máquinas con acceso a internet y credenciales de
identidad BSV**; por eso la ejecución real queda pendiente de un entorno con
esos recursos. Todo lo verificable en un solo host ya está automatizado
(`tests/test_resilience.py`, `scripts/run_resilience_wan.py`,
`scripts/resilience_netem.sh`).

## Topología

```
  Worker A (NAT)  --bitbeam per-pair ECDH-->  Coordinator (NAT, puerto publicado)
  Worker B (NAT)  --bitbeam per-pair ECDH-->     |-- HTTP 127.0.0.1:8095
                                                |-- túnel id local 127.0.0.1:18080
```

El coordinador no necesita IP pública: el túnel de identidad (`tunnel-server-id`)
escucha en la DHT por pubkey y reenvía a `127.0.0.1:8095`. Cada worker abre un
`tunnel-client` con `--coordinator-pubkey`, generando un beam ECDH por par.

## 0. Preparación (en cada máquina)

```bash
# identidad BSV por nodo (crea runs/identity/<name>.json)
.venv/bin/python scripts/identity_init.py --name coordinator
.venv/bin/python scripts/identity_init.py --name worker-a
.venv/bin/python scripts/identity_init.py --name worker-b

# publicar los pubkeys de los workers en un archivo de allowlist para el coordinador
# (uno por línea); se exporta automáticamente a checkpoint_dir/allowed_peer_pubkeys.txt
```

El fichero de allowlist se mantiene sincronizado de forma atómica/idempotente
(`diloco/tunnel_allowlist.py`) y el túnel aplica altas/bajas en caliente.

## 1. Coordinador (máquina C)

```bash
.venv/bin/python scripts/run_coordinator.py \
  --config configs/docker-coordinator.json \
  --coordinator-identity runs/identity/coordinator.json \
  --p2p-topic didiloco-<run-id> \
  --p2p-context didiloco-<run-id> \
  --allow-pubkey-file /ruta/a/workers-pubkeys.txt \
  --require-node-identity \
  --print-invite runs/invite.json \
  --checkpoint-dir runs/wan-run \
  --no-reset
```

El invite firmado (`runs/invite.json`) se distribuye a los workers; contiene
topic/config y evita pegar secretos en comandos.

## 2. Workers (máquinas A y B)

```bash
COORD_PUBKEY=$(.venv/bin/python -c "import json;print(json.load(open('runs/identity/coordinator.json'))['pubkey'])")

.venv/bin/python scripts/run_worker.py \
  --config configs/docker-coordinator.json \
  --node-id worker-a --shard-id 0 \
  --p2p-topic didiloco-<run-id> \
  --p2p-context didiloco-<run-id> \
  --coordinator-pubkey "$COORD_PUBKEY" \
  --node-identity runs/identity/worker-a.json \
  --invite runs/invite.json \
  --state-cache-dir runs/state-cache \
  --log runs/wan-run/worker-a.jsonl
```

Repetir con `--node-id worker-b --shard-id 1` en la máquina B.

## 3. Rehearsal local (sin multi-máquina)

Antes de la validación real, reproducir degradación de enlace y churn en un host:

```bash
# suite determinista in-process + E2E a través del proxy WAN
./scripts/resilience_netem.sh

# perfil más agresivo
DELAY_MS=120 JITTER_MS=40 LOSS=0.05 ROUNDS=6 \
  ./scripts/resilience_netem.sh

# si la máquina tiene root + tc: añadir impairment a nivel kernel en lo
sudo ./scripts/resilience_netem.sh --netem
```

## 4. Métricas a recoger (aceptación)

- `/v1/status` y `/v1/health`: rondas completadas, `failure`, `waiting_workers`,
  `effective_round_timeout_s`, `partial`.
- Logs de worker: `stale_round_retry`, re-registros tras 403, reintentos de submit,
  `state_response_bytes` / `upload_bytes` y `sync_fraction`.
- Sidecar: eventos `peer-listener-added`, `peers-updated`, `peer-connected`,
  `peer-disconnected`, `peer-listener-removed` y reconexiones del `tunnel-client`.
- TTFB y fallos DHT si se instrumenta el gateway.

**Criterio de aceptación P2.4:** con 2 workers tras NAT, al menos 6 rondas
completadas sin intervención manual; un worker reiniciado a mitad de run se
reincorpora y convergen en ≤ 2 rondas; sin corrupción de estado (hashes
verificados) y sin exposición de secretos en logs/UI.

## Estado

- Implementado y verificado localmente: suite de resiliencia, proxy WAN,
  `tc netem` opcional, allowlist↔túnel atómica, revocación en caliente,
  re-registro y recuperación ante reinicio del coordinador.
- **Pendiente (requiere infra externa):** ejecutar los pasos 1–2 en máquinas
  separadas tras NAT y capturar las métricas del apartado 4.
