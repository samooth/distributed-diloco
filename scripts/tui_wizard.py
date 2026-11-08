#!/usr/bin/env python3
"""Curses-based TUI wizard for DiLoCo (no external deps).

Arrow keys navigate, Enter selects, q quits. Falls back to input() if no tty.
"""
from __future__ import annotations

import curses
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _has_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def select(stdscr, title: str, options: list[str], default: int = 0) -> int:
    curses.curs_set(0)
    idx = default
    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        stdscr.addstr(0, 0, title, curses.A_BOLD)
        for i, opt in enumerate(options):
            marker = "▶ " if i == idx else "  "
            attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
            stdscr.addstr(2 + i, 2, marker + opt, attr)
        stdscr.addstr(h - 2, 0, "↑/↓ navegar · Enter seleccionar · q salir", curses.A_DIM)
        stdscr.refresh()
        key = stdscr.getch()
        if key == curses.KEY_UP:
            idx = (idx - 1) % len(options)
        elif key == curses.KEY_DOWN:
            idx = (idx + 1) % len(options)
        elif key in (curses.KEY_ENTER, 10, 13):
            return idx
        elif key in (ord("q"), ord("Q")):
            raise SystemExit(0)


def text_input(stdscr, prompt: str, default: str = "") -> str:
    curses.curs_set(1)
    h, w = stdscr.getmaxyx()
    stdscr.clear()
    stdscr.addstr(0, 0, prompt, curses.A_BOLD)
    stdscr.addstr(2, 0, f"default: {default}" if default else "")
    stdscr.refresh()
    # Use a simple input via curses.getstr
    stdscr.addstr(4, 0, "> ")
    stdscr.refresh()
    buf = stdscr.getstr(4, 2, 256).decode("utf-8", errors="replace").strip()
    curses.curs_set(0)
    return buf or default


def run_curses(stdscr) -> dict:
    role_opts = ["coordinator", "worker", "both"]
    role = role_opts[select(stdscr, "Rol a ejecutar", role_opts, 2)]

    model_opts = ["toy", "hf"]
    model_kind = model_opts[select(stdscr, "Tipo de modelo", model_opts, 1)]

    model_path = ""
    if model_kind == "hf":
        model_path = text_input(stdscr, "Ruta del modelo", "/path/to/model")

    data_path = text_input(stdscr, "Ruta dataset JSONL", "data/zig_sample.jsonl")
    eval_path = text_input(stdscr, "Ruta holdout JSONL", "data/zig_holdout_sample.jsonl")
    topic = text_input(stdscr, "Sala Hyperswarm", "diloco-run")
    token = text_input(stdscr, "Token", "change-me-now")
    device_opts = ["cpu", "cuda"]
    device = device_opts[select(stdscr, "Device", device_opts, 1)]
    H = int(text_input(stdscr, "Pasos locales H", "4") or "4")
    rounds = int(text_input(stdscr, "Rondas", "3") or "3")
    workers = int(text_input(stdscr, "Workers", "2") or "2")
    port = int(text_input(stdscr, "Puerto coordinador", "8095") or "8095")
    run_id = text_input(stdscr, "Nombre de la run", "diloco-run")

    return {
        "role": role,
        "model_kind": model_kind,
        "model_path": model_path,
        "data_path": data_path,
        "eval_path": eval_path,
        "topic": topic,
        "token": token,
        "device": device,
        "H": H,
        "rounds": rounds,
        "workers": workers,
        "port": port,
        "run_id": run_id,
    }


def generate_config(cfg: dict) -> Path:
    run_id = cfg["run_id"]
    model_kind = cfg["model_kind"]
    if model_kind == "hf":
        model = {
            "kind": "hf",
            "name_or_path": cfg["model_path"],
            "rank": 8,
            "alpha": 16.0,
            "dtype": "auto",
            "quant": "nf4",
            "target_modules": ["q_proj", "v_proj"],
            "max_length": 128,
            "gradient_checkpointing": True,
        }
    else:
        model = {"kind": "toy", "in_features": 16, "out_features": 8, "rank": 4, "alpha": 4.0}

    config = {
        "run_id": run_id,
        "model": model,
        "train": {
            "H": cfg["H"],
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
            "path": cfg["data_path"],
            "eval_path": cfg["eval_path"],
            "num_shards": max(cfg["workers"], 4),
            "max_samples": 64 if model_kind == "hf" else 0,
            "eval_samples": 8 if model_kind == "hf" else 64,
            "eval_every": 1,
            "shuffle": True,
        },
        "coordinator": {
            "host": "0.0.0.0",
            "port": cfg["port"],
            "min_workers": cfg["workers"],
            "max_workers": max(cfg["workers"], 4),
            "quorum": cfg["workers"],
            "round_timeout_s": 600.0,
            "max_rounds": cfg["rounds"],
            "checkpoint_dir": f"runs/{run_id}",
            "checkpoint_every": 1,
            "token": cfg["token"],
            "idle_timeout_s": 300.0,
            "allow_late_join": True,
        },
    }

    out = PROJECT_ROOT / "configs" / f"{run_id}.json"
    out.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return out


def main() -> int:
    if not _has_tty():
        print("No hay TTY; usa scripts/wizard.py")
        return 1

    cfg = curses.wrapper(run_curses)
    out = generate_config(cfg)
    print(f"\nConfig guardada en: {out}")
    print(f"Ejecuta: .venv/bin/python scripts/run_coordinator.py --config configs/{cfg['run_id']}.json --p2p-topic {cfg['topic']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
