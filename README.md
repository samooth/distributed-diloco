# DiLoCo WAN

**Entrena adaptadores LoRA de 32B/70B sobre internet, sin datacenter.**

DiLoCo WAN adapta modelos grandes usando **DiLoCo** (*Distributed Low-Communication*):
sincroniza solo el adaptador cada `H` pasos locales, con un optimizador exterior
Nesterov. La base queda congelada en NF4 (QLoRA), así cada isla cabe en una
GPU doméstica (8–80 GB) y el tráfico por ronda baja de 140 GB a 0.07–0.8 GB.

## Características

- **Backend toy (CPU) y HF (PEFT/QLoRA)** con carga thread-safe.
- **Coordinador HTTP/gRPC** con token, quórum, deadline, heartbeat, late join.
- **Compresión int8** (4×), **top-k** + **error feedback**.
- **Churn completo**: rejoin, evicción, recuperación ≤ 2 rondas (test integrado).
- **TLS/mTLS**, object store local/S3, emulación WAN, túnel Hyperswarm.
- **Gateway WebSocket** (Node.js) para discovery/relay sin IPs.
- **Consola web** (`web/dashboard.html`) y **monitor de red** (`web/network.html`).
- **Wizard de terminal** (`scripts/wizard.py`).
- **39 tests OK**.

## Arquitectura

```
        ┌─────────────────┐
        │  Coordinador    │  agregación Nesterov · checkpoints · métricas
        └────────┬────────┘
                 │  HTTP/1.0 → gRPC/HTTP2+TLS → Hyperswarm
   ┌─────────────┼─────────────┐
   │             │             │
 ┌─▼─┐         ┌─▼─┐         ┌─▼─┐
 │ A │         │ B │         │ C │   islas: base NF4 + LoRA local
 └───┘         └───┘         └───┘
```

Una ronda: entrena `H` pasos → calcula `Δ = θ_global − θ_local` → sube delta
comprimido → agrega → Nesterov → nuevo adaptador → checkpoint.

## Inicio rápido

```bash
# Tests
.venv/bin/python -m unittest discover -s tests -q

# Smoke (toy, 2 islas, 4 rondas)
.venv/bin/python scripts/smoke_local.py --reset --rounds 4 --workers 2 --device cpu

# Wizard interactivo
.venv/bin/python scripts/wizard.py
```

Para HF (4B):
```bash
.venv/bin/python scripts/smoke_local.py \
  --config configs/qwen3-4b-single.json --workers 1 --rounds 1 --device cuda
```

## Resultados (resumen)

| Config | H=10 | H=20 | H=50 | H=100 |
|---|---|---|---|---|
| int8 | +5.29% | +1.23% | -0.97% | -1.47% |
| int8+top-k 10% | +7.72% | +6.46% | +4.45% | +3.14% |

loss gap = (DiLoCo − baseline)/baseline. Negativo = DiLoCo mejor.
Toy: DiLoCo `0.003723` vs baseline `0.008095` (−54%).

## Documentación

- [Onboarding](docs/ONBOARDING.md) — de cero a tu primera ronda.
- [Algoritmo](docs/01-algoritmo.md) · [Arquitectura](docs/02-arquitectura.md) · [Comunicación](docs/03-comunicacion.md)
- [Modelos](docs/04-modelos.md) · [Entrenamiento](docs/05-entrenamiento.md) · [Operaciones](docs/06-operaciones.md)
- [Seguridad](docs/07-seguridad.md) · [Experimentos](docs/08-experimentos.md) · [Referencias](docs/09-referencias.md)
- [Changelog](CHANGELOG.md) · [Estado](STATUS.md) · [Roadmap](ROADMAP.md)

## Presentación web

Abre `web/index.html` (landing) o `web/dashboard.html` (consola) en el navegador.

## Licencia

Ver `LICENSE`.