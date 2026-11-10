# Onboarding — DiLoCo WAN

Guía práctica para poner en marcha un entrenamiento distribuido con **DiLoCo WAN**. El objetivo es automatizar lo máximo posible (wrappers, autodetección, bootstrap local) y moverse hacia un modelo de identidad **BSV + bitbeam** en lugar de Nostr.

## 1. Conceptos
- **Isla**: nodo/worker con base congelada (NF4/QLoRA) y adaptador LoRA local.
- **Coordinador**: agrega deltas cada `H` pasos, aplica Nesterov outer, publica estado global y checkpoints.
- **Túnel P2P**: `--p2p-topic` usa **bitbeam v4/DHT** multiplexado (una conexión por worker). Cifrado por canal; la autorización de aplicación sigue siendo necesaria.
- **Identidad (objetivo)**: claves **BSV** por nodo (`@bsv/sdk`), canales bitbeam por par con derivación ECDH (`{from,to}`), allowlist del coordinador y firma/verificación de mensajes (registro/deltas/manifiestos).

## 2. Instalación rápida
```bash
git clone <repo-url> distributed-diloco
cd distributed-diloco
# Entorno Python
.venv/bin/python -m pip install -r requirements.txt  # según tu setup
# Sidecar Node (para túnel P2P)
cd network/holepunch-sidecar && npm install && cd ../..
```
Wrappers (usan `.venv/bin/python` si existe):
```bash
./coordinator --help
./worker --help
./agent --help
```

## 3. Verificación
```bash
.venv/bin/python -m unittest discover -s tests -q  # 39 OK
.venv/bin/python scripts/smoke_local.py --reset --rounds 4 --workers 2 --device cpu
```

## 4. Configuración (sin rutas personales)
Los configs del repo usan **placeholders**. Copia a un config local **no versionado**:
```bash
cp configs/qwen3-4b-2nodes.json configs/local/qwen3-4b-2nodes.json  # o configs/local-*.json
```
Edita `model.name_or_path`, `data.path`/`data.eval_path`, `coordinator.token`, etc. Requisito HF: `data.num_shards >= coordinator.max_workers`.

## 5. Identidad BSV + bitbeam
Sigue el patrón de meshllm: **descubrimiento/atado al canal por claves**, no solo por un secreto compartido.

### Estado actual (MVP de admisión)
- Genera identidades: `scripts/identity_init.py --name node-0` → `runs/identity/node-0.json` (pubkey + WIF local, 600).
- Worker envía `node_pubkey` en `/v1/register` con `--node-identity runs/identity/node-0.json`.
- Coordinador aplica allowlist/admisión: `--allow-pubkey <hex>`, `--allow-pubkey-file <file>` (líneas o JSON), `--require-node-identity`, o `coordinator.allowed_node_pubkeys` / `coordinator.require_node_identity` en config.
- Si la pubkey no está permitida o falta cuando es requerida → `403`.

Ejemplo:
```bash
./coordinator --config <local>.json --allow-pubkey-file runs/identity/node-0.json --allow-pubkey-file runs/identity/node-1.json --require-node-identity --p2p-topic diloco-run-4b ...
./worker --config <local>.json --node-id node-0 --node-identity runs/identity/node-0.json --p2p-topic diloco-run-4b ...
```

### Objetivo (siguientes pasos)
- **Canal por par**: `BitBeam({from: workerPriv, to: coordPub}, { context: runId })` (ECDH) en vez de topic compartido.
- **Firma/verificación** de `register`, deltas y manifiestos con `@bsv/sdk`.
- **Invite firmado** por el coordinador (pubkey, bootstrap/topic, política, expiración) para unirse con un flag/comando.
- LAN/mDNS opcional; WAN por DHT/bitbeam.

## 6. Estado inicial HF
```bash
# Exportar (si no existe)
.venv/bin/python scripts/export_initial_state.py \
  --config configs/local/qwen3-4b-2nodes.json \
  --out runs/init-qwen3-4b-2nodes --device cuda
# O auto
./coordinator --config configs/local/qwen3-4b-2nodes.json --init-state auto ...
```
El worker autodetecta `runs/init-<config-stem>` y hace **bootstrap local** si los hashes coinciden (evita descargar `/v1/state` grande).

## 7. Lanzar
**Directo (LAN/Tailscale) — más determinista hoy:**
```bash
./coordinator --config configs/local/qwen3-4b-2nodes.json --checkpoint-dir runs/piloto --max-rounds 3 --no-wait
./worker --config configs/local/qwen3-4b-2nodes.json --node-id node-0 --shard-id 0 --coordinator http://<IP>:8095 --device cuda
```
**P2P sin IPs (bitbeam v4):**
```bash
cd network/holepunch-sidecar && npm install
./coordinator --config configs/local/qwen3-4b-2nodes.json --checkpoint-dir runs/piloto --max-rounds 3 --no-wait --p2p-topic diloco-run-4b
./worker --config configs/local/qwen3-4b-2nodes.json --node-id node-0 --shard-id 0 --p2p-topic diloco-run-4b --tunnel-listen-port 18080 --device cuda
```
Tras cambios en el sidecar, **reinicia** coordinador/workers.

## 8. Dashboard + agent
```bash
./agent --open  # sirve web/ en http://127.0.0.1:8765/dashboard.html
```
Rellena URL del coordinador y token. El agent permite lanzar coordinador/worker desde la UI (entorno confiable). También puedes servir `web/` con un servidor HTTP estático.

## 9. Solución de problemas
- **Rutas personales**: usa config local fuera del repo/versionado.
- **`num_shards >= max_workers`**: ajusta `data.num_shards`.
- **Falta init-state HF**: `runs/init-<config-stem>` o `--init-state auto`.
- **`IncompleteRead`/truncado**: usa init-state local (hash compatible) o transporte directo.
- **Túnel falla**: `npm install` en sidecar, Node 18+, mismo topic, reiniciar sidecar, probar directo.
- **403/401**: token/hash base/config distintos; revisar allowlist.
- **Rejoin/churn**: el sistema soporta rejoin de nodos conocidos (recuperación orientativa ≤ 2 rondas).

## 10. Documentación
- `README.md`, `STATUS.md`, `ROADMAP.md`, `CHANGELOG.md`
- `docs/03-comunicacion.md` (protocolo + bitbeam v4/mux + identidad BSV objetivo)
- `docs/07-seguridad.md`, `docs/06-operaciones.md`
- `docs/09-referencias.md`

**Principio:** UX tipo meshllm (instalar, configurar, descubrir/unirse con un comando), pero transporte/identidad **BSV + bitbeam** en lugar de Nostr.
