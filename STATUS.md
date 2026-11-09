# Estado actual

Snapshot operativo del repo. Histórico detallado: `CHANGELOG.md`.

## Resumen
- **Tests**: `39 OK` (`.venv/bin/python -m unittest discover -s tests -q`).
- **Backends**: `toy` (CPU determinista) + `hf` (PEFT/QLoRA, thread-safe).
- **Coordinación**: HTTP y gRPC opcional, token, quorum, deadline, heartbeat, late join, churn/rejoin, checkpoints atómicos.
- **Transporte P2P**: sidecar Node con túnel **bitbeam v4** (DHT) + multiplexado persistente para `--p2p-topic`; legado Hyperswarm en modos auxiliares (`announce/share` según sidecar).
- **Bootstrap HF**: `--init-state` con autodetección `runs/init-<config-stem>`, `--init-state auto` en coordinador (exporta si falta), worker usa init local si `state_hash/base_hash/config_hash` coinciden (evita bajar `/v1/state` grande).
- **UX**: wrappers `./coordinator`, `./worker`, `./agent`; agent sirve `web/` y `--open`; wizard/TUI; consola web y monitor.

## Implementado
- P0/P1: config/fingerprint, backends, coordinador, compresión (int8, top-k, error feedback), checkpoints/reanudación.
- P2: rejoin/churn (≤ 2 rondas en test), evicción, TLS/mTLS opcional, object store local/S3, emulación WAN, late join.
- HF: carga thread-safe, exportación de estado inicial, validación de base/config/hash.
- API: `/v1/health`, `/v1/status` (alias `/v1/metrics`, incluye `config_hash`), `/v1/state_meta`, `/v1/state`, `/v1/register`, `/v1/heartbeat`, `/v1/delta`, `/v1/leave`.
- Sidecar `network/holepunch-sidecar`: `tunnel-server`/`tunnel-client` sobre bitbeam v4 con mux de substreams TCP; requiere `npm install`.
- Worker: reintentos en requests seguras ante `IncompleteRead`/desconexiones.

## Resultados (referencia)
- F1 toy: DiLoCo `0.003723` vs baseline `0.008095` (`loss_gap=-54%`).
- Qwen2.5-0.5B (2 workers, 5 rondas): int8 H=10/20/50/100 → `+5.29%`, `+1.23%`, `-0.97%`, `-1.47%`; int8+top-k 10% → `+7.72%`, `+6.46%`, `+4.45%`, `+3.14%`.
- Smoke HF 4B (QLoRA NF4) validado en hardware local (ajustar rutas/modelos por máquina).

## Pendiente / siguiente
- Validación E2E del túnel bitbeam en NAT real/multi-máquina y reconexión automática del beam.
- Identidad BSV por nodo (`@bsv/sdk` + bitbeam `{from,to}`), allowlist y firma/verificación de mensajes.
- `pilot/up` de 1 comando (prepare + launch + join card + dashboard prellenado).
- Barridos WAN/`tc netem`, métricas Prometheus, pilotos 32B/70B.
- Endurecer `scripts/agent.py` (auth/allowlist) antes de exponer fuera de entornos confiables.

## Comandos útiles
```bash
.venv/bin/python -m unittest discover -s tests -q
./coordinator --config configs/smoke.json --device cpu --reset --max-rounds 4 --no-wait
./worker --config configs/smoke.json --node-id node-0 --coordinator http://127.0.0.1:8095 --device cpu

# HF (rutas/modelos en config local; init-state auto-descubierto en runs/init-<config>)
./coordinator --config configs/qwen3-4b-2nodes.json --checkpoint-dir runs/qwen3-4b-2nodes --max-rounds 3 --no-wait --p2p-topic diloco-run-4b
./worker --config configs/qwen3-4b-2nodes.json --node-id node-0 --shard-id 0 --p2p-topic diloco-run-4b --tunnel-listen-port 18080 --device cuda

./agent --open
```

## Entorno / notas
- Usa `.venv` del repo. Los configs del repo usan **placeholders** (sin rutas personales); ajusta paths localmente o usa un config local no versionado.
- Túnel P2P: Node.js + `cd network/holepunch-sidecar && npm install` (y deps nativas de bitbeam/dht si el entorno lo requiere).
- `--init-state auto` en coordinador puede cargar el modelo base para exportar; en 32B/70B prefiere exportar en una máquina con el modelo y copiar el bundle.
