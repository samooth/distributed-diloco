# ◆ DiLoCo WAN

> **Entraînez des adaptateurs LoRA pour grands modèles sur Internet, sans datacenter.**

DiLoCo WAN utilise **DiLoCo** (*Distributed Low-Communication*) : synchronisation uniquement de l'adaptateur LoRA tous les `H` pas locaux avec un optimiseur externe Nesterov. La base reste gelée (NF4/QLoRA).

## Fonctionnalités
- **Backends** : `toy` (CPU déterministe) + `hf` (PEFT/QLoRA).
- **Coordinateur** : HTTP/gRPC, token, quorum, deadline, heartbeat, late join, `/v1/state_meta`.
- **Compression** : int8 (4×), top-k + error feedback.
- **Churn** : rejoin, éviction, récupération ≤ 2 rounds (testé).
- **P2P/WAN** : tunnel **bitbeam v4** (DHT) multiplexé persistant (`--p2p-topic`), émulation WAN.
- **Bootstrap HF** : découverte auto `runs/init-<config-stem>`, `--init-state auto`, bootstrap local si les hashs correspondent.
- **UX** : wrappers `./coordinator` `./worker` `./agent`, wizards, console/moniteur web.

## Démarrage rapide
```bash
.venv/bin/python -m unittest discover -s tests -q
./coordinator --config configs/smoke.json --device cpu --reset --max-rounds 4 --no-wait
./worker --config configs/smoke.json --node-id node-0 --coordinator http://127.0.0.1:8095 --device cpu
./agent --open
```
Tunnel P2P : `cd network/holepunch-sidecar && npm install`, puis `--p2p-topic <topic>`. Les configs du dépôt utilisent des chemins génériques.

## Documentation
- [Onboarding](../ONBOARDING.md) · [Communication](../03-comunicacion.md) · [Sécurité](../07-seguridad.md)
- [Changelog](../../CHANGELOG.md) · [Statut](../../STATUS.md) · [Roadmap](../../ROADMAP.md)
