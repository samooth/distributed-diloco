# Changelog

Todas las notas detalladas de cambios, resultados y decisiones del proyecto
se registran aquí. El `README.md` es la presentación; este archivo es el
histórico técnico.

## [Unreleased] — estado actual

### Implementado
- P0/P1: config validada con fingerprint, backend `toy`/`hf` (PEFT/QLoRA),
  coordinador HTTP/gRPC, checkpoints, reanudación, exportación inicial.
- P2.1: compresión int8 por tensor, top-k, error feedback.
- P2.2: rejoin, late join, evicción, TLS/mTLS, object store, WAN emulation,
  transporte P2P Hyperswarm, gRPC/mTLS real, churn completo (≤ 2 rondas).
- Carga thread-safe del modelo HF.
- `run_coordinator.py --reset`.
- Wizard de terminal (`scripts/wizard.py`).
- Gateway DiLoCo (`network/diloco-gateway/`).
- Adaptador Python `diloco/gateway_client.py`.
- Consola web (`web/`) y monitor de red (`web/network.html`).
- Página de presentación (`web/index.html`).
- Onboarding (`docs/ONBOARDING.md`).

### Resultados
- F1 toy: DiLoCo `0.003723` vs baseline `0.008095`, `loss_gap=-54%`.
- F1 real (Qwen2.5-0.5B, 2 workers, 5 rondas):
  - int8 H=10/20/50/100: `+5.29%`, `+1.23%`, `-0.97%`, `-1.47%`.
  - int8+top-k 10%: `+7.72%`, `+6.46%`, `+4.45%`, `+3.14%`.
  - int8+error feedback H=10/20/50: `+7.44%`, `+6.02%`, `+3.62%`.
- Payload Qwen2.5-0.5B: 2.16 MB → 0.54 MB (4×) con int8.
- 4B (Qwen3-4B-Thinking-2507) smoke OK en RTX 3080 8 GB.

### Tests
- 39 tests OK.

### Pendiente
- Barrido int8+EF a H=100 (3 semillas).
- Pruebas WAN con `tc netem` y medición CU/sync.
- Piloto 32B y 70B QLoRA.
- Full fine-tuning 7B/13B (FSDP2).
- 8–20 nodos / 100B+ (investigación).

### Decisiones clave
- Algoritmo: DiLoCo (AdamW interior + Nesterov exterior).
- Solo adaptador LoRA/DoRA se sincroniza.
- Base en NF4 congelada.
- Topología estrella con coordinador para 2–4 islas.
- Rondas síncronas con deadline y checkpoint por ronda.
- Stack Python/PyTorch/PEFT/bitsandbytes.

### Riesgos
- Subida doméstica satura el coordinador → anillo/árbol, int8, H mayor.
- 70B no cabe en 48 GB con seq_len alto → seq_len 512–1024, batch 1.
- Nodo lento bloquea la ronda → deadline, expulsión, rejoin.
- Base distinta entre nodos → hash de base/config, fallar cerrado.

### Comandos útiles
```bash
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python scripts/smoke_local.py --reset --rounds 4 --workers 2 --device cpu
.venv/bin/python scripts/wizard.py
```

### Referencias esenciales
- DiLoCo: https://arxiv.org/abs/2311.08105
- OpenDiLoCo: https://arxiv.org/abs/2407.07852
- INTELLECT-1: https://arxiv.org/abs/2412.01152
- Streaming DiLoCo: https://arxiv.org/abs/2501.18512
- SparseLoCo: https://arxiv.org/abs/2508.15706
- DiLoCoX: https://arxiv.org/abs/2506.21263

Lista completa en `docs/09-referencias.md`.