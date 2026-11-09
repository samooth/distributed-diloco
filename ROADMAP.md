# Roadmap de implementación

Leyenda: `[x]` hecho, `[~]` parcial, `[ ]` pendiente.

## P0 — Especificación y esqueleto
- [x] Configuración validada con fingerprint.
- [x] Documentación base y estructura del repo.

## P1 — Backend, coordinador y 2 islas
- [x] Backend `toy` determinista y `hf` PEFT/QLoRA.
- [x] Serialización solo de parámetros entrenables.
- [x] Coordinador HTTP/gRPC con token, quórum, deadline, heartbeat.
- [x] Agregación Nesterov, checkpoints atómicos, reanudación.
- [x] Exportación previa del adaptador inicial.
- [x] Smoke local y validación HF pequeña.
- [x] Autodescubrimiento/init-state (`runs/init-<config>`, `--init-state auto` en coordinador, bootstrap local en worker).

## P2.1 — Compresión y eficiencia de sync
- [x] int8 por tensor, top-k, error feedback.
- [x] Barridos iniciales H/int8/top-k.
- [x] Compresión de transporte vs compresión DiLoCo separadas conceptualmente (delta int8/top-k; transporte confiable por túnel/HTTP).
- [ ] zstd/codec negociado para bootstrap de estado grande.
- [ ] Barrido int8+EF con más semillas y `sync_fraction` medido.

## P2.2 — Robustez, red y transporte
- [x] Rejoin/late join, evicción, `/v1/leave`, retención, object store.
- [x] Emulación WAN.
- [x] Túnel P2P por `--p2p-topic` (bitbeam v4 + mux persistente) y modos auxiliares sidecar.
- [x] `/v1/state_meta` y bootstrap local para evitar descarga completa del estado.
- [x] Reintentos worker para requests seguras.
- [~] gRPC/mTLS real (existe; integración/UX puede ampliarse).
- [ ] Validación E2E del túnel bitbeam en NAT/Internet real, reconexión, métricas de latencia/bytes.
- [ ] Integración completa gateway WebSocket como transporte primario opcional.
- [ ] Descarga de estado chunked/reanudable o diff por hash/ronda.

## Seguridad e identidad
- [x] Token coordinador, TLS/mTLS opcional, validación de base/config/hash.
- [~] Agent local para launcher (sirve web, sin auth fuerte; solo entornos confiables).
- [ ] Auth/allowlist/rate limit para agent y hardening de gateway.
- [ ] Identidad BSV por nodo con `@bsv/sdk` + bitbeam `{from,to}`, allowlist por pubkey.
- [ ] Firma/verificación de registros, deltas y manifiestos de checkpoint.

## UX / DX
- [x] Wrappers raíz: `./coordinator`, `./worker`, `./agent`.
- [x] Agent sirve `web/` y `--open`.
- [x] Wizards (clásico/TUI) y consola web.
- [ ] `pilot/up` 1-comando (preflight, export, launch, join card, dashboard prellenado).
- [ ] Logs en vivo, perfiles locales gitignored, i18n completa de UI.
- [ ] `make setup`/script de instalación (Python + Node sidecar).

## F1–F6 (experimentos y escala)
- [x] F1 toy y barridos HF pequeños iniciales.
- [ ] 3 semillas/config clave, `tc netem`, `sync_fraction <= 20%`.
- [ ] F2 4 islas 7B/8B con churn/red real.
- [ ] F3 32B QLoRA, F4 70B QLoRA.
- [ ] F5 full fine-tuning 7B/13B (FSDP2), F6 escala/investigación.
