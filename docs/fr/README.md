# ◆ DiLoCo WAN

> **Entraînez des adaptateurs LoRA 32B/70B sur Internet, sans datacenter.**

DiLoCo WAN adapte de grands modèles avec **DiLoCo** (*Distributed Low-Communication*) :
il synchronise uniquement l'adaptateur tous les `H` pas locaux, avec un optimiseur
extérieur Nesterov. La base reste gelée en NF4 (QLoRA), chaque île tient sur un
GPU domestique (8–80 Go) et le trafic par round passe de **140 Go à 0.07–0.8 Go**.

## Fonctionnalités

- **Backends** : `toy` (CPU déterministe) + `hf` (PEFT/QLoRA), chargement thread-safe.
- **Coordinateur** : HTTP/gRPC, token, quorum, deadline, heartbeat, late join.
- **Compression** : int8 (4×), top-k + error feedback.
- **Churn** : rejoin, éviction, récupération ≤ 2 rounds (test intégré).
- **Sécurité** : TLS/mTLS, allowlist, validation des deltas.
- **Stockage** : checkpoints atomiques, rétention, object store local/S3.
- **WAN** : émulation (latence, jitter, perte, bande passante), tunnel Hyperswarm.
- **Web** : page de présentation + console opérationnelle + moniteur réseau (responsive).
- **CLI** : wizard classique et TUI (curses).
- **Agent** : lancement de processus depuis l'interface web.

## Démarrage rapide

```bash
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python scripts/smoke_local.py --reset --rounds 4 --workers 2 --device cpu
.venv/bin/python scripts/wizard.py
```

## Documentation

- [Onboarding](../ONBOARDING.md)
- [Algorithme](../01-algoritmo.md) · [Architecture](../02-arquitectura.md) · [Communication](../03-comunicacion.md)
- [Modèles](../04-modelos.md) · [Entraînement](../05-entrenamiento.md) · [Opérations](../06-operaciones.md)
- [Sécurité](../07-seguridad.md) · [Expériences](../08-experimentos.md) · [Références](../09-referencias.md)
- [Changelog](../../CHANGELOG.md) · [Statut](../../STATUS.md) · [Roadmap](../../ROADMAP.md)