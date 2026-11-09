# Onboarding — DiLoCo WAN

Guía práctica para correr tu primera ronda (local y multi-nodo).

## 1. Qué es
DiLoCo sincroniza **solo el adaptador LoRA** cada `H` pasos locales. La base queda congelada (NF4/QLoRA). Un coordinador agrega deltas (outer Nesterov) y publica el nuevo estado global.

## 2. Requisitos
- **Python 3.11–3.13** y `.venv` en el repo (o `python3` disponible).
- **Toy/CPU**: no requiere GPU.
- **HF/GPU**: PyTorch CUDA, `transformers`, `peft`, `bitsandbytes` (según config).
- **Túnel P2P (`--p2p-topic`)**: Node.js 18+ y dependencias del sidecar:
  ```bash
  cd network/holepunch-sidecar && npm install
  ```
- Modelos/datos: ajusta rutas en tu config. **Los configs del repo usan placeholders**; no se versionan rutas personales.

## 3. Preparar entorno
```bash
git clone <repo-url> distributed-diloco
cd distributed-diloco
# crea/usa .venv según tu entorno
.venv/bin/python -m pip install -r requirements.txt  # si aplica en tu setup
```
Wrappers convenientes (usan `.venv/bin/python` si existe):
```bash
./coordinator --help
./worker --help
./agent --help
```

## 4. Verificar
```bash
.venv/bin/python -m unittest discover -s tests -q  # 39 OK esperado
.venv/bin/python scripts/smoke_local.py --reset --rounds 4 --workers 2 --device cpu
```

## 5. Configs y rutas
- No edites configs versionados con rutas personales. Copia a un config local (idealmente no versionado) o edita localmente con cuidado:
  ```bash
  cp configs/qwen3-4b-2nodes.json configs/local-qwen3-4b-2nodes.json
  # edita model.name_or_path y data.path/eval_path
  ```
- Requisito HF: `data.num_shards >= coordinator.max_workers`.
- Token: `coordinator.token` debe coincidir entre coordinador y workers; cámbialo para redes reales (ideal: env/archivo local).

## 6. Estado inicial HF (`init-state`)
Para backends HF, el coordinador necesita el bundle del adaptador inicial (evita cargar la base solo para shapes).
- **Auto**: el coordinador busca `runs/init-<config-stem>`. Puedes pasar `--init-state auto` para exportarlo si falta (carga el modelo base).
- **Manual** (recomendado para modelos grandes):
  ```bash
  ./coordinator  # no; usa el script de export:
  .venv/bin/python scripts/export_initial_state.py \
    --config configs/local-qwen3-4b-2nodes.json \
    --out runs/init-qwen3-4b-2nodes --device cuda
  ```
- **Worker**: autodetecta `runs/init-<config-stem>`. Si el `state_hash/base_hash/config_hash` coincide con el coordinador, **no descarga** `/v1/state` completo (útil con túnel/WAN).

## 7. Piloto HF 2 nodos (directo)
Coordinador:
```bash
./coordinator --config configs/local-qwen3-4b-2nodes.json \
  --checkpoint-dir runs/qwen3-4b-2nodes --max-rounds 3 --no-wait
```
Workers (misma máquina o IP alcanzable):
```bash
./worker --config configs/local-qwen3-4b-2nodes.json \
  --node-id node-0 --shard-id 0 --coordinator http://<IP_COORD>:8095 --device cuda
./worker --config configs/local-qwen3-4b-2nodes.json \
  --node-id node-1 --shard-id 1 --coordinator http://<IP_COORD>:8095 --device cuda
```

## 8. Multi-nodo sin IPs (bitbeam v4, `--p2p-topic`)
Usa el mismo `--p2p-topic` en todos. El sidecar crea un túnel **bitbeam v4/DHT multiplexado persistente** (una conexión P2P por worker).

```bash
./coordinator --config configs/local-qwen3-4b-2nodes.json \
  --checkpoint-dir runs/qwen3-4b-2nodes --max-rounds 3 --no-wait \
  --p2p-topic diloco-run-4b

./worker --config configs/local-qwen3-4b-2nodes.json \
  --node-id node-0 --shard-id 0 \
  --p2p-topic diloco-run-4b --tunnel-listen-port 18080 --device cuda
```
Notas:
- Tras actualizar `network/holepunch-sidecar/index.js`, reinicia coordinador/workers.
- Si el DHT tarda o el bootstrap es grande, usa `--init-state` local y/o transporte directo seguro (Tailscale/TLS).

## 9. Dashboard / agent
```bash
./agent --open   # sirve web/ y abre http://127.0.0.1:8765/dashboard.html
```
En el dashboard configura URL del coordinador (`http://127.0.0.1:8095` o túnel local), token, y (opcional) URL del agent para lanzar procesos. El agent es para **entornos confiables** (sin auth fuerte por defecto).

También puedes servir `web/` manualmente:
```bash
cd web && python3 -m http.server 8000
```

## 10. Troubleshooting
- **`num_shards must be >= coordinator.max_workers`**: ajusta `data.num_shards`.
- **HF pide exportar init state**: crea/descubre `runs/init-<config-stem>` o pasa `--init-state auto`.
- **`IncompleteRead`/desconexión bajando estado**: usa `--init-state` local (mismo hash) o transporte directo; el worker reintenta requests seguras.
- **Túnel no conecta**: verifica `npm install` en `network/holepunch-sidecar`, Node 18+, mismo topic, coordinador reiniciado con sidecar actual; prueba directo para aislar.
- **`401/403`**: token incorrecto, base/config hash distinto, o worker expulsado/stale (revisa logs; el worker reintenta/recupera en casos soportados).
- **Rutas de modelo/datos**: usa paths locales en un config no versionado.

## 11. Seguridad e identidad (estado)
- Token + validación `config_hash`/`base_hash`/`state_hash`; TLS/mTLS opcional.
- Transporte: bitbeam v4 cifra el canal (DHT/Noise). Identidad BSV por nodo (claves con `@bsv/sdk`, `BitBeam {from,to}`), allowlist por pubkey y firma/verificación de mensajes están en roadmap.

## 12. Referencias
- `README.md`, `STATUS.md`, `ROADMAP.md`, `CHANGELOG.md`
- `docs/03-comunicacion.md` (protocolo/transporte), `docs/07-seguridad.md`
- `docs/09-referencias.md`
