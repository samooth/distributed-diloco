# ◆ DiLoCo WAN

> **Entrena adaptadores LoRA de 32B/70B sobre internet, sin datacenter.**

[![Tests](https://img.shields.io/badge/tests-39%20OK-brightgreen)](#)
[![Python](https://img.shields.io/badge/python-3.11%E2%80%933.13-blue)](#)
[![PyTorch](https://img.shields.io/badge/pytorch-2.8%2B-orange)](#)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](#)

DiLoCo WAN adapta modelos grandes usando **DiLoCo** (*Distributed Low-Communication*):
sincroniza solo el adaptador cada `H` pasos locales, con un optimizador exterior
Nesterov. La base queda congelada en NF4 (QLoRA), así cada isla cabe en una
GPU doméstica (8–80 GB) y el tráfico por ronda baja de **140 GB a 0.07–0.8 GB**.

---

## ✨ Características

| | |
|---|---|
| 🧠 **Backend** | `toy` (CPU determinista) + `hf` (PEFT/QLoRA), carga thread-safe |
| 🌐 **Coordinador** | HTTP/gRPC, token, quórum, deadline, heartbeat, late join |
| 📦 **Compresión** | int8 (4×), top-k + error feedback |
| 🔄 **Churn** | rejoin, evicción, recuperación ≤ 2 rondas (test integrado) |
| 🔐 **Seguridad** | TLS/mTLS, allowlist, validación de deltas |
| 💾 **Storage** | checkpoints atómicos, retención, object store local/S3 |
| 🌍 **WAN** | emulación (delay, jitter, pérdida, ancho de banda), túnel Hyperswarm |
| 🖥️ **Web** | landing + consola operativa + monitor de red (responsive) |
| ⌨️ **CLI** | wizard clásico y TUI (curses) |
| 🤖 **Agent** | lanzador de procesos desde la web UI |

## 🏗️ Arquitectura

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

**Una ronda:** entrena `H` pasos → `Δ = θ_global − θ_local` → sube delta
comprimido → agrega → Nesterov → nuevo adaptador → checkpoint.

## 🚀 Inicio rápido

```bash
# Tests
.venv/bin/python -m unittest discover -s tests -q

# Smoke (toy, 2 islas, 4 rondas)
.venv/bin/python scripts/smoke_local.py --reset --rounds 4 --workers 2 --device cpu

# Wizard (clásico o TUI)
.venv/bin/python scripts/wizard.py
.venv/bin/python scripts/tui_wizard.py
```

**HF 4B:**
```bash
.venv/bin/python scripts/smoke_local.py \
  --config configs/qwen3-4b-single.json --workers 1 --rounds 1 --device cuda
```

## 📊 Resultados (resumen)

| Config | H=10 | H=20 | H=50 | H=100 |
|---|---|---|---|---|
| int8 | +5.29% | +1.23% | -0.97% | -1.47% |
| int8+top-k 10% | +7.72% | +6.46% | +4.45% | +3.14% |

> loss gap = (DiLoCo − baseline)/baseline. Negativo = DiLoCo mejor.
> Toy: DiLoCo `0.003723` vs baseline `0.008095` (−54%).

## 🌐 Web

- `web/index.html` — presentación.
- `web/dashboard.html` — consola operativa (KPIs, nodos, rondas, logs, launcher).
- `web/network.html` — monitor de red (topología, peers Hyperswarm).

Abre con un servidor HTTP: `cd web && .venv/bin/python -m http.server 8000`.

## 📚 Documentación

- [Onboarding](docs/ONBOARDING.md) — de cero a tu primera ronda.
- [Algoritmo](docs/01-algoritmo.md) · [Arquitectura](docs/02-arquitectura.md) · [Comunicación](docs/03-comunicacion.md)
- [Modelos](docs/04-modelos.md) · [Entrenamiento](docs/05-entrenamiento.md) · [Operaciones](docs/06-operaciones.md)
- [Seguridad](docs/07-seguridad.md) · [Experimentos](docs/08-experimentos.md) · [Referencias](docs/09-referencias.md)
- [Changelog](CHANGELOG.md) · [Estado](STATUS.md) · [Roadmap](ROADMAP.md)

## 📄 Licencia

Ver `LICENSE`.