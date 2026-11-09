# ◆ DiLoCo WAN

> 🌍 [English](docs/en/README.md) · [Español](README.md) · [Français](docs/fr/README.md) · [中文](docs/zh/README.md) · [日本語](docs/ja/README.md)

> **Entrena adaptadores LoRA de 32B/70B sobre internet, sin datacenter.**

[![Tests](https://img.shields.io/badge/tests-39%20OK-brightgreen)](#)
[![Python](https://img.shields.io/badge/python-3.11%E2%80%933.13-blue)](#)
[![PyTorch](https://img.shields.io/badge/pytorch-2.8%2B-orange)](#)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](#)

DiLoCo WAN adapta modelos grandes usando **DiLoCo** (*Distributed Low-Communication*):
sincroniza solo el adaptador LoRA cada `H` pasos locales con optimizador exterior
Nesterov. La base queda congelada (NF4/QLoRA), cada isla cabe en una GPU doméstica
(8–80 GB) y el tráfico por ronda baja drásticamente frente a replicar pesos completos.

---

## ✨ Características

| | |
|---|---|
| 🧠 **Backend** | `toy` (CPU determinista) + `hf` (PEFT/QLoRA), carga thread-safe |
| 🌐 **Coordinador** | HTTP/gRPC, token, quorum, deadline, heartbeat, late join, `/v1/state_meta` |
| 📦 **Compresión** | int8 (4×), top-k + error feedback |
| 🔄 **Churn** | rejoin, evicción, recuperación ≤ 2 rondas (test integrado) |
| 🔐 **Seguridad** | TLS/mTLS opcional, validación base/config/hash; identidad BSV/bitbeam en roadmap |
| 💾 **Storage** | checkpoints atómicos, retención, object store local/S3 |
| 🌍 **P2P/WAN** | túnel **bitbeam v4** (DHT) multiplexado persistente (`--p2p-topic`), emulación WAN |
| 🖥️ **Web** | landing + consola operativa + monitor de red |
| ⌨️ **CLI/UX** | wrappers `./coordinator` `./worker` `./agent`, wizards, autodetección de init-state |

## 🏗️ Arquitectura

```
        ┌─────────────────┐
        │  Coordinador    │  Nesterov outer · checkpoints · métricas
        └────────┬────────┘
                 │  HTTP/gRPC → TLS opcional → bitbeam v4/DHT (túnel mux)
   ┌─────────────┼─────────────┐
   │             │             │
 ┌─▼─┐         ┌─▼─┐         ┌─▼─┐
 │ A │         │ B │         │ C │   base congelada + LoRA local
 └───┘         └───┘         └───┘
```

**Ronda:** `H` pasos locales → `Δ` del adaptador → compresión/subida → agregación → Nesterov → estado global → checkpoint.

## 🚀 Inicio rápido

```bash
# Tests
.venv/bin/python -m unittest discover -s tests -q

# Smoke (toy, CPU)
.venv/bin/python scripts/smoke_local.py --reset --rounds 4 --workers 2 --device cpu

# Wrappers
./coordinator --config configs/smoke.json --device cpu --reset --max-rounds 4 --no-wait
./worker --config configs/smoke.json --node-id node-0 --coordinator http://127.0.0.1:8095 --device cpu

# Web/agent (sirve web/ y abre navegador)
./agent --open
```

**HF (4B, ejemplo):** ajusta rutas/modelos en un config local (los configs del repo usan placeholders). Para HF el coordinador necesita el bundle inicial: autodetecta `runs/init-<config-stem>` o usa `--init-state auto` (exporta; puede cargar el modelo base).
```bash
./coordinator --config configs/qwen3-4b-2nodes.json --checkpoint-dir runs/qwen3-4b-2nodes --max-rounds 3 --no-wait --p2p-topic diloco-run-4b
./worker --config configs/qwen3-4b-2nodes.json --node-id node-0 --shard-id 0 --p2p-topic diloco-run-4b --tunnel-listen-port 18080 --device cuda
```
Si el worker tiene el mismo `init-state` local (hash compatible), no descarga el estado global completo.

## 🌍 P2P (bitbeam v4)
```bash
cd network/holepunch-sidecar && npm install
```
El túnel es persistente y multiplexado (no crea un lookup por request). Requiere Node.js y conectividad DHT. Para bootstrap grande inestable, usa `--init-state` local o transporte directo/Tailscale.

## 📚 Documentación
- [Onboarding](docs/ONBOARDING.md) — de cero a tu primera ronda.
- [Algoritmo](docs/01-algoritmo.md) · [Arquitectura](docs/02-arquitectura.md) · [Comunicación](docs/03-comunicacion.md)
- [Modelos](docs/04-modelos.md) · [Entrenamiento](docs/05-entrenamiento.md) · [Operaciones](docs/06-operaciones.md)
- [Seguridad](docs/07-seguridad.md) · [Experimentos](docs/08-experimentos.md) · [Referencias](docs/09-referencias.md)
- [Changelog](CHANGELOG.md) · [Estado](STATUS.md) · [Roadmap](ROADMAP.md)

## 📄 Licencia
MIT (`LICENSE`).
