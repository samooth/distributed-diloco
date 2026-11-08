# Onboarding: de cero a tu primera ronda DiLoCo

Esta guía está pensada para alguien que **no conoce el proyecto**. Síguela en orden.

## 1. ¿Qué es esto?

DiLoCo WAN entrena adaptadores LoRA de modelos grandes (32B/70B) usando
varios nodos conectados por internet. En lugar de sincronizar todo el modelo
cada paso, sincroniza solo el adaptador cada `H` pasos (DiLoCo). Esto reduce
el tráfico de red ~500× y tolera nodos que aparecen/desaparecen.

El repo incluye:
- Coordinador (Python, HTTP/gRPC).
- Workers (Python, PyTorch + PEFT/QLoRA).
- Compresión int8, top-k, error feedback.
- Tolerancia a fallos (churn, late join, evicción).
- Emulación WAN y transporte P2P (Hyperswarm).
- Consola web y wizard de terminal.

## 2. Requisitos mínimos

- **Python 3.11–3.13**
- **GPU NVIDIA** con 8 GB+ VRAM (para 4B). Para 32B/70B: 24–80 GB.
- **Disco:** 20 GB libres (modelos) + 10 GB (checkpoints).
- **RAM:** 16 GB mínimo.
- **Red:** conexión estable; para multi-nodo, Tailscale/Netbird recomendado.

## 3. Clonar el repo

```bash
git clone <URL-del-repo> distributed-diloco
cd distributed-diloco
```

## 4. Entorno Python

El entorno virtual usado en desarrollo está en:
```
.venv
```
Si no existe, créalo:
```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install torch transformers peft bitsandbytes accelerate safetensors datasets
```

Para el resto de esta guía usaremos `PYTHON=.venv/bin/python`.

## 5. Modelos y datos

Descarga o ubica los modelos en tu directorio de modelos. Ejemplo:
- `/ruta/a/Qwen2.5-0.5B-Instruct` (validación rápida)
- `/ruta/a/Qwen3-4B-Thinking-2507` (pruebas serias)

Dataset Zig:
- `/ruta/a/data/zig_dense_v5_module_clean.jsonl`
- `/ruta/a/data/zig_holdout.jsonl`

Ajusta las rutas en los configs (`model.name_or_path`, `data.path`, `data.eval_path`)
a tu instalación.

## 6. Verificar instalación

```bash
$PYTHON -m unittest discover -s tests -q
# Esperado: "Ran 39 tests ... OK"
```

## 7. Primera prueba (toy, sin modelo grande)

```bash
$PYTHON scripts/smoke_local.py --reset --rounds 4 --workers 2 --device cpu
```
Deberías ver 4 rondas completadas y `loss` decreciente.

## 8. Primera prueba con modelo real (4B)

```bash
$PYTHON scripts/smoke_local.py \
  --config configs/qwen3-4b-single.json --workers 1 --rounds 1 --device cuda
```
Esperado: `rounds_done=1`, `done=true`, checkpoint guardado.

## 9. Wizard (recomendado para no teclear)

```bash
$PYTHON scripts/wizard.py
```
Te guía para generar un config y los comandos exactos.

## 10. Multi-nodo con un colega

1. Ambos clonan el repo y comparten el mismo modelo/dataset.
2. Cambian el `token` en `configs/qwen3-4b-2nodes.json`.
3. Uno lanza el coordinador:
   ```bash
   $PYTHON scripts/run_coordinator.py --config configs/qwen3-4b-2nodes.json --device cuda --reset --max-rounds 3 --no-wait
   ```
4. Cada uno lanza su worker:
   ```bash
   $PYTHON scripts/run_worker.py --config configs/qwen3-4b-2nodes.json --node-id node-0 --shard-id 0 --coordinator http://<IP>:8095 --device cuda
   ```
5. Monitorean en `web/dashboard.html`.

Para evitar IPs, usen Hyperswarm:
```bash
# coordinador
$PYTHON scripts/run_coordinator.py --config configs/qwen3-4b-2nodes.json --device cuda --reset --max-rounds 3 --no-wait --p2p-topic diloco-run-4b
# workers
$PYTHON scripts/run_worker.py --config configs/qwen3-4b-2nodes.json --node-id node-0 --shard-id 0 --p2p-topic diloco-run-4b --tunnel-listen-port 18080 --device cuda
```

## 11. Consola web

Abre `web/index.html` o `web/dashboard.html` en el navegador. No requiere servidor.
Conecta a la URL del coordinador y el token.

## 12. Dónde leer más

| Documento | Contenido |
|---|---|
| `README.md` | Visión general, estado, decisiones |
| `STATUS.md` | Estado actual y resultados |
| `ROADMAP.md` | Fases y pendientes |
| `docs/01-algoritmo.md` | DiLoCo, pseudo-gradientes |
| `docs/02-arquitectura.md` | Componentes y tolerancia a fallos |
| `docs/03-comunicacion.md` | Protocolo y compresión |
| `docs/09-referencias.md` | Papers y repos |

## 13. Glosario rápido

- **Isla:** unidad de sincronización (1 GPU o nodo multi-GPU).
- **H:** pasos locales antes de sincronizar.
- **Pseudo-gradiente:** `θ_global − θ_local`.
- **Outer optimizer:** Nesterov sobre pseudo-gradientes agregados.
- **Churn:** nodos que entran/salen a mitad de ronda.
- **loss gap:** (DiLoCo − baseline) / baseline (negativo = DiLoCo mejor).

## 14. Problemas comunes

- **CUDA out of memory:** baja `max_length`, `batch_size`, o usa `quant: "nf4"`.
- **Modelo no encontrado:** verifica la ruta en el config.
- **Token inválido (401):** el token del worker debe coincidir con el del coordinador.
- **Nodo evicted:** aumenta `idle_timeout_s` o reduce el tiempo de entrenamiento por ronda.

---

¡Bienvenido! Empieza por el paso 7 (smoke toy) para validar tu entorno.