# Estado actual

Snapshot operativo del repo. Histórico detallado: `CHANGELOG.md`.

## Resumen
- **Tests**: `39 OK` (`.venv/bin/python -m unittest discover -s tests -q`).
- **Backends**: `toy` (CPU determinista) + `hf` (PEFT/QLoRA, thread-safe).
- **Coordinación**: HTTP y gRPC opcional, token, quorum, deadline, heartbeat, late join, churn/rejoin, checkpoints atómicos.
- **Transporte P2P**: sidecar Node con túnel **bitbeam v4** (DHT) + multiplexado persistente para `--p2p-topic`; fallback por topic compartido y modo por par ECDH con identidad BSV (`--coordinator-identity/--node-identity/--coordinator-pubkey`).
- **Bootstrap HF**: `--init-state` con autodetección `runs/init-<config-stem>`, `--init-state auto` en coordinador (exporta si falta), worker usa init local si `state_hash/base_hash/config_hash` coinciden (evita bajar `/v1/state` grande).
- **Identidad BSV (MVP)**: keygen `@bsv/sdk`, registro con `node_pubkey` + firma ECDSA verificada, allowlist/admisión (`--allow-pubkey*`, `--require-node-identity`), firma de updates (meta) verificada por coordinador.
- **UX**: wrappers `./coordinator`, `./worker`, `./agent`, `./pilot`; `--print-invite`/`--invite` (firmado), agent sirve `web/` y `--open` (POST protegible con `--token`); wizard/TUI; consola web y monitor.

## Implementado
- P0/P1: config/fingerprint, backends, coordinador, compresión (int8, top-k, error feedback), checkpoints/reanudación.
- P2: rejoin/churn (≤ 2 rondas en test), evicción, TLS/mTLS opcional, object store local/S3, emulación WAN, late join.
- HF: carga thread-safe, exportación de estado inicial, validación de base/config/hash.
- API: `/v1/health`, `/v1/status` (alias `/v1/metrics`, incluye `config_hash`), `/v1/state_meta`, `/v1/state`, `/v1/register`, `/v1/heartbeat`, `/v1/delta`, `/v1/leave`.
- Sidecar `network/holepunch-sidecar`: `tunnel-server`/`tunnel-client` sobre bitbeam v4 con mux de substreams TCP; modo identidad por par (ECDH BSV, context); requiere `npm install`.
- Worker: reintentos en requests seguras ante `IncompleteRead`/desconexiones; firma de registro/updates cuando hay identidad.
- Coordinador: verificación de firma de registro/updates, allowlist por pubkey, rechazo 403 en identidad inválida/no autorizada.

## Resultados (referencia)
- F1 toy: DiLoCo `0.003723` vs baseline `0.008095` (`loss_gap=-54%`).
- Qwen2.5-0.5B (2 workers, 5 rondas): int8 H=10/20/50/100 → `+5.29%`, `+1.23%`, `-0.97%`, `-1.47%`; int8+top-k 10% → `+7.72%`, `+6.46%`, `+4.45%`, `+3.14%`.
- Smoke HF 4B (QLoRA NF4) validado en hardware local (ajustar rutas/modelos por máquina).

## Pendiente / siguiente
- Validación E2E del túnel bitbeam (topic e identidad por par) en NAT real/multi-máquina, reconexión automática del beam y métricas.
- Firma de manifiestos/checkpoints y política de firmas más estricta/end-to-end; rotación/revocación de claves y protección operativa de WIF.
- Soporte dinámico de peers en túnel identidad del coordinador (late join/allowlist dinámica) o fallback coordinado; descarga de estado chunked/reanudable.
- Completar `pilot.py` (preflight más profundo, launch de workers/dashboard prellenado, join card) y UX web para invites/identidades.
- Hardening: auth completa y allowlist de acciones/configs en `scripts/agent.py`, reducción de CORS en entornos no locales, rate limiting.
- Barridos WAN/`tc netem`, métricas Prometheus, pilotos 32B/70B.

## Comandos útiles
```bash
.venv/bin/python -m unittest discover -s tests -q
./pilot preflight --config configs/smoke.json

# Identidades + invite
.venv/bin/python scripts/identity_init.py --name coordinator
.venv/bin/python scripts/identity_init.py --name node-0
./coordinator --config <local>.json --coordinator-identity runs/identity/coordinator.json \
  --allow-pubkey-file runs/identity/node-0.json --require-node-identity \
  --p2p-topic diloco-run-4b --print-invite runs/invite.json --max-rounds 3 --no-wait
./worker --config <local>.json --node-id node-0 --shard-id 0 --device cuda \
  --node-identity runs/identity/node-0.json --invite runs/invite.json

./agent --open
```

## Entorno / notas
- Usa `.venv` del repo. Los configs del repo usan **placeholders** (sin rutas personales); ajusta paths localmente o usa un config local no versionado.
- Túnel P2P: Node.js + `cd network/holepunch-sidecar && npm install` (y deps nativas de bitbeam/dht si el entorno lo requiere).
- `--init-state auto` en coordinador puede cargar el modelo base para exportar; en 32B/70B prefiere exportar en una máquina con el modelo y copiar el bundle.
- `runs/identity/*.json` contienen WIF privado (gitignored bajo `runs/`); no compartir.
- `scripts/agent.py` permite control de procesos; usar `--token` y bind local en entornos no totalmente confiables.
