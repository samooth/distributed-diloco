#!/usr/bin/env python3
"""Interactive wizard to generate a DiLoCo config and run commands.

Minimizes manual typing: prompts for model path, data path, Hyperswarm topic,
token, ports and device, then writes a ready-to-use config and prints the exact
commands to run the coordinator and/or workers.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt, IntPrompt
    RICH = True
    _console = Console()
except Exception:  # pragma: no cover
    RICH = False
    _console = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def ask(prompt: str, default: str = "") -> str:
    if RICH:
        value = Prompt.ask(prompt, default=default or None)
        return value or default
    suffix = f" [{default}]" if default else ""
    value = input(f"{CYAN}{prompt}{suffix}:{RESET} ").strip()
    return value or default


def ask_choice(prompt: str, choices: list[str], default: str = "") -> str:
    print(f"{CYAN}{prompt}{RESET} ({'/'.join(choices)})")
    while True:
        value = ask(">", default).lower()
        if value in [c.lower() for c in choices]:
            return value
        print(f"{YELLOW}Opción inválida. Elige una de: {', '.join(choices)}{RESET}")


def ask_path(prompt: str, must_exist: bool = True) -> str:
    while True:
        value = ask(prompt)
        if not value:
            print(f"{YELLOW}Ruta vacía.{RESET}")
            continue
        path = Path(value).expanduser()
        if must_exist and not path.exists():
            print(f"{YELLOW}No existe: {path}{RESET}")
            continue
        return str(path.resolve())


def ask_int(prompt: str, default: int) -> int:
    if RICH:
        return IntPrompt.ask(prompt, default=default)
    while True:
        value = ask(prompt, str(default))
        try:
            return int(value)
        except ValueError:
            print(f"{YELLOW}Debe ser un entero.{RESET}")


def main() -> int:
    if RICH:
        _console.print(Panel.fit("◆ DiLoCo Wizard\nGenera una configuración y los comandos para coordinador/workers.", title="Wizard"))
    else:
        print(f"\n{BOLD}◆ DiLoCo Wizard{RESET}")
        print("Genera una configuración y los comandos para coordinador/workers.\n")

    role = ask_choice("¿Qué rol vas a ejecutar?", ["coordinator", "worker", "both"], "both")

    model_kind = ask_choice("Tipo de modelo", ["toy", "hf"], "hf")
    if model_kind == "hf":
        model_path = ask_path("Ruta del modelo (ej. /ruta/a/Qwen3-4B-Thinking-2507)")
    else:
        model_path = ""

    data_path = ask_path("Ruta del dataset JSONL", must_exist=True)
    eval_path = ask_path("Ruta del holdout JSONL", must_exist=True)

    transport = ask_choice("Transporte", ["direct", "hyperswarm"], "hyperswarm")
    topic = ask("Nombre de la sala Hyperswarm", "diloco-run") if transport == "hyperswarm" else ""
    token = ask("Token del coordinador", "change-me-now")
    device = ask_choice("Device", ["cpu", "cuda"], "cuda")
    H = ask_int("Pasos locales por ronda (H)", 4)
    rounds = ask_int("Número de rondas", 3)
    workers = ask_int("Número de workers", 2)
    port = ask_int("Puerto del coordinador", 8095)

    run_id = ask("Nombre de la run", "diloco-run")
    checkpoint_dir = f"runs/{run_id}"

    config = {
        "run_id": run_id,
        "model": {
            "kind": model_kind,
            "name_or_path": model_path if model_kind == "hf" else None,
            "rank": 8,
            "alpha": 16.0,
            "dtype": "auto",
            "quant": "nf4" if model_kind == "hf" else "none",
            "target_modules": ["q_proj", "v_proj"],
            "max_length": 128,
            "gradient_checkpointing": True,
        } if model_kind == "hf" else {
            "kind": "toy",
            "in_features": 16,
            "out_features": 8,
            "rank": 4,
            "alpha": 4.0,
        },
        "train": {
            "H": H,
            "inner_lr": 0.0001 if model_kind == "hf" else 0.05,
            "inner_weight_decay": 0.0,
            "outer_lr": 0.2,
            "outer_momentum": 0.9,
            "batch_size": 1 if model_kind == "hf" else 16,
            "grad_accum": 1,
            "max_grad_norm": 1.0,
            "max_delta_norm": 0.0,
            "seed": 42,
            "reset_inner_optimizer": True,
            "delta_compression": "int8",
            "error_feedback": True,
        },
        "data": {
            "path": data_path,
            "eval_path": eval_path,
            "num_shards": max(workers, 4),
            "max_samples": 64 if model_kind == "hf" else 0,
            "eval_samples": 8 if model_kind == "hf" else 64,
            "eval_every": 1,
            "shuffle": True,
        },
        "coordinator": {
            "host": "0.0.0.0",
            "port": port,
            "min_workers": workers,
            "max_workers": max(workers, 4),
            "quorum": workers,
            "round_timeout_s": 600.0,
            "max_rounds": rounds,
            "checkpoint_dir": checkpoint_dir,
            "checkpoint_every": 1,
            "token": token,
            "idle_timeout_s": 300.0,
            "allow_late_join": True,
        },
    }

    out_path = PROJECT_ROOT / "configs" / f"{run_id}.json"
    out_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"\n{GREEN}Config guardada en:{RESET} {out_path}")

    py = sys.executable
    print(f"\n{BOLD}Comandos:{RESET}\n")

    if role in ("coordinator", "both"):
        if model_kind == "hf":
            init_dir = f"runs/init-{run_id}"
            print(f"# 0. Exportar adaptador inicial (solo la primera vez)")
            print(f"{py} scripts/export_initial_state.py --config configs/{run_id}.json --out {init_dir} --device {device}\n")
            print(f"# 1. Coordinador")
            if transport == "hyperswarm":
                print(f"{py} scripts/run_coordinator.py --config configs/{run_id}.json --init-state {init_dir} --checkpoint-dir {checkpoint_dir} --max-rounds {rounds} --no-wait --p2p-topic {topic} --device {device}")
            else:
                print(f"{py} scripts/run_coordinator.py --config configs/{run_id}.json --init-state {init_dir} --checkpoint-dir {checkpoint_dir} --max-rounds {rounds} --no-wait --device {device}")
        else:
            print(f"# 1. Coordinador")
            if transport == "hyperswarm":
                print(f"{py} scripts/run_coordinator.py --config configs/{run_id}.json --checkpoint-dir {checkpoint_dir} --max-rounds {rounds} --no-wait --p2p-topic {topic} --device {device}")
            else:
                print(f"{py} scripts/run_coordinator.py --config configs/{run_id}.json --checkpoint-dir {checkpoint_dir} --max-rounds {rounds} --no-wait --device {device}")
        print()

    if role in ("worker", "both"):
        for i in range(workers):
            print(f"# Worker {i}")
            if transport == "hyperswarm":
                print(f"{py} scripts/run_worker.py --config configs/{run_id}.json --node-id node-{i} --shard-id {i} --p2p-topic {topic} --tunnel-listen-port {18080 + i} --device {device} --log {checkpoint_dir}/logs/node-{i}.jsonl")
            else:
                print(f"{py} scripts/run_worker.py --config configs/{run_id}.json --node-id node-{i} --shard-id {i} --coordinator http://<IP-coordinador>:{port} --device {device} --log {checkpoint_dir}/logs/node-{i}.jsonl")
            print()

    print(f"{GREEN}Consola web:{RESET} abre web/dashboard.html y conecta a http://<ip-coordinador>:{port} con el token.\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelado.")
        raise SystemExit(130)
