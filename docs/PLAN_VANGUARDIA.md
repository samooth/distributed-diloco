# Plan de vanguardia — DiLoCo WAN (BSV + bitbeam)

Plan vivo para convertir `distributed-diloco` en un sistema distribuido de entrenamiento **robusto, verificable y con UX de clase mundial** para LoRA/QLoRA 32B–70B sobre WAN.

**Regla operativa:** avanzar en orden de prioridad, y tras cada bloque de progreso relevante **desplegar/actualizar** (`NGINX_CONF_FILES="" NGINX_CONTAINER="" ./deploy/deploy_vps.sh` para publicar código/web + contenedores; usar `deploy/deploy.sh` si solo hace falta rsync+compose).

**Estado base (referencia):** tests `39 OK`, coordinador/worker HTTP+gRPC opcional, compresión int8/top-k+EF, churn/rejoin, túnel bitbeam v4, identidad BSV MVP (admisión/firma), invite firmado, `/v1/public/status`, web pública en `didiloco.bsv.direct`, dashboard agente con auth HTTP Basic/token.

Leyenda: `[x]` hecho · `[~]` en progreso/parcial · `[ ]` pendiente

---

## 1. Visual y experiencia de producto (PRIORIDAD 1) — en curso
Objetivo: que el proyecto se perciba “kickass” desde el primer contacto: estado público, dashboard operativo y narrativa técnica claros, modernos y confiables.

### 1.1 Web pública de estado (`web/public/index.html`, `didiloco.bsv.direct`)
- [x] Endpoint sanitizado `/v1/public/status` + fallback resiliente en nginx (ya operativo).
- [~] Rediseño visual premium: hero/identidad BSV+bitbeam, layout claro, animaciones sutiles, tipografía y jerarquía.
- [~] Visualización de estado más rica: progreso, métricas clave, última ronda, estado de red, indicadores en vivo, manejo elegante de “sin datos”.
- [ ] Histórico agregado público (sparklines/tendencias) cuando el endpoint exponga series; degradar con gracia si no hay datos.
- [ ] Secciones informativas: cómo funciona DiLoCo, stack (bitbeam/BSV, LoRA), seguridad/privacidad, CTA para unirse/contribuir (según política).
- [ ] Accesibilidad, responsive, performance (sin dependencias pesadas innecesarias), i18n/es-en opcional.

### 1.2 Dashboard/console de agente (`web/dashboard.*`, `web/index.html`, `web/network.*`)
- [~] Pulido visual y consistencia (tema, componentes, tablas, empty states, toasts, charts).
- [ ] UX de conexión/operación: wizard claro, estados de auth/agente, feedback de errores, prefill seguro, separación coordinador vs agente.
- [ ] Launcher seguro y usable: formularios/perfiles (no prompts hardcodeados), validación, logs/estado de procesos en vivo, confirmaciones.
- [ ] Vistas de red/topología y salud (workers, rondas, latencia/sync, túneles) con datos reales disponibles.
- [ ] Observabilidad en UI: eventos, timeline de rondas, export/reportes, modo oscuro/claro consistente.

### 1.3 Marca, docs visuales y narrativa
- [ ] README/landing (`web/index.html`) alineados: demo visual, arquitectura, resultados, quickstart real.
- [ ] Assets ligeros (diagramas SVG, capturas/gifs opcionales) y guía de despliegue pública/privada clara.

**Criterio de avance 1:** web pública y dashboard se ven profesionales en desktop/móvil, comunican estado real y no exponen datos sensibles. **Deploy tras cada mejora visible.**

---

## 2. Robustez WAN y transporte (PRIORIDAD 2)
Objetivo: entrenamiento estable sobre internet real (NAT, cortes, latencia variable).

- [~] **Estado chunked + reanudable + verificado**: `/v1/state` con HTTP Range/reanudación, cache de blob en coordinador y metadatos en `state_meta` (`state_blob_hash/state_blob_bytes`, `state_blob_chunk_size`, `state_blob_chunk_hashes`, `state_blob_merkle_root`, `state_etag`). Worker: descarga HTTP por chunks alineados al manifiesto, reintentos/resume, detección de cambio de estado durante descarga, verificación por chunk + hash/Merkle/blob, **reuso en memoria** y **cache persistente local por `state_hash`** (`runs/state-cache` o `--state-cache-dir`) para evitar re-descargas tras reinicios (HTTP y gRPC si expone `state_meta`). Coordinador expone `ETag`/`X-DiLoCo-{Round,State-Hash,Blob-Hash}` y `If-None-Match` (304). Pruebas de manifest/range/resume/reensamblado/416 y cache persistente. Pendiente: paridad completa en gRPC para descarga chunked/artefactos y extensión a checkpoints/artefactos grandes.
- [ ] **Compresión negociada**: `zstd`/codecs para bootstrap y deltas; medir bytes on-wire y `sync_fraction`.
- [~] **Reconexión automática E2E**: worker con reintentos/backoff en submit (errores transitorios 0/502/503/504), recuperación de updates duplicados ambiguos, re-registro ante pérdida de sesión, y espera idempotente de avance de ronda; sidecar `tunnel-client` bitbeam con reconexión automática/backoff manteniendo el listener local. Pendiente: validación E2E WAN/NAT completa, re-discovery dinámica y hardening adicional de heartbeat/rejoin bajo reinicios prolongados.
- [ ] **Validación NAT/Internet**: E2E multi-máquina (topic y túnel por par), métricas de conexión (TTFB, reconexiones, fallos DHT), fallback coordinado.
- [ ] **Túnel identidad dinámico**: alta/baja de peers y late join sin reiniciar coordinador (allowlist/sesiones en caliente).
- [ ] **Manejo de stragglers**: timeouts adaptativos, políticas de quorum/parcialidad seguras, reporting claro.
- [ ] **Suite de resiliencia**: pruebas con `tc netem` (latencia/jitter/pérdida/ancho de banda) y churn inducido.

**Criterio:** un run sobre WAN/NAT tolera desconexiones y reanudaciones sin intervención manual ni corrupción de estado.

---

## 3. Seguridad, identidad y confianza (PRIORIDAD 3)
Objetivo: confianza criptográfica y operación segura (alineado a BSV + bitbeam).

- [ ] **Protocolo firmado end-to-end**: registro, updates, manifiestos de ronda, checkpoints/estado e invites encadenados/verificables (hash previo, políticas estrictas).
- [ ] **Gestión de claves**: rotación/revocación, identidades por run/delegadas, expiración de invites/tokens, protección de WIF/secrets (no material sensible en runtime/logs/UI).
- [ ] **Autorización reforzada**: allowlist dinámica segura, políticas por nodo/acción, auditoría de eventos de seguridad.
- [ ] **Hardening agente/dashboard**: rate limiting, CSRF/origin checks, allowlist de configs/acciones, separación lectura/control, ejecución solo de perfiles permitidos, logs de auditoría.
- [ ] **Transporte directo endurecido**: mTLS/gRPC first-class con UX simple; límites y validaciones de payload/tamaño/forma.
- [ ] **Reproducibilidad verificable**: manifiestos firmados (commit, config_hash, state/base hash, params, resultados) y run registry.

**Criterio:** toda transición crítica de estado es autenticada, autorizada y verificable.

---

## 4. Eficiencia, ML y escala 32B/70B (PRIORIDAD 4)
Objetivo: entrenamientos grandes eficientes y medibles.

- [ ] **Sync eficiente**: zstd + packing, evaluación de compresión efectiva, presupuesto de `sync_fraction`.
- [ ] **Algoritmos/estrategias**: sparsificación adaptativa, overlap cómputo-comunicación, variantes async/streaming (según viabilidad), ajuste dinámico de `H`/quorum.
- [ ] **Evaluación por ronda**: métricas/benchmarks automáticos, selección de mejor checkpoint, comparación vs baseline.
- [ ] **Recetas 32B/70B**: QLoRA/NF4 probados, perfiles de memoria/VRAM, particionado de datos, guías reproducibles.
- [ ] **Benchmarks publicados**: 3+ semillas, perfiles WAN, tablas (loss, tiempo a objetivo, coste de sync, bytes).

**Criterio:** runs 32B/70B reproducibles con coste de sync controlado y calidad medida.

---

## 5. Observabilidad y operación (PRIORIDAD 5)
Objetivo: operar, depurar y confiar en la red.

- [ ] **Métricas**: Prometheus `/metrics` (rondas, quorum, p50/p99, retries, bytes, cómputo vs sync, fallos por causa).
- [ ] **Logs/trazas**: estructurados con `run_id/round/node`, correlación, OpenTelemetry opcional.
- [ ] **Estado público enriquecido**: agregados históricos/salud sin datos sensibles.
- [ ] **Artefactos**: integración S3-compatible/object store robusta, retención y políticas de checkpoints.

**Criterio:** cualquier incidente de run se diagnostica con métricas/logs y evidencia verificable.

---

## 6. UX/DX “one-command” y adopción (PRIORIDAD 6)
Objetivo: onboarding y operación tan simples como potentes.

- [ ] **`pilot up` completo**: preflight, identidad, export/init, launch coord/workers, join card/QR/invite, dashboard prellenado.
- [ ] **Instalación simple**: `make setup` o script único (Python + sidecar + checks), perfiles locales gitignored.
- [ ] **Imágenes listas**: Docker GPU (CUDA+HF/PEFT+sidecar) y flujos dev/prod claros.
- [ ] **Descubrimiento LAN opcional** (mDNS) manteniendo modelo BSV+bitbeam para WAN.
- [ ] **DX**: mejores errores, docs ejecutables, ejemplos por escala.

**Criterio:** un nuevo colaborador levanta un run funcional siguiendo pasos mínimos y claros.

---

## 7. Diferenciadores BSV: procedencia e incentivos (PRIORIDAD 7)
Objetivo: convertir la red en infraestructura colaborativa verificable.

- [ ] **Recibos de contribución firmados**: pruebas por ronda (trabajo útil aceptado, hashes, métricas) y recibo agregado del coordinador.
- [ ] **Anclaje en BSV**: hash de manifiestos/checkpoints/hitos para procedencia pública y auditoría.
- [ ] **Micropagos/incentivos opcionales**: liquidación por contribución verificada con identidad BSV (diseño con controles anti-abuso).

**Criterio:** procedencia y contribuciones demostrables; incentivos alineados y seguros.

---

## 8. Secuencia de ejecución recomendada
1. **Visual/producto** (1.1 → 1.2 → 1.3), con deploys frecuentes.  ← *comenzar aquí*
2. **Robustez WAN**: chunked/resumable + reconexión + NAT E2E (2).
3. **Seguridad/verificabilidad**: firmado E2E + hardening + claves (3).
4. **Eficiencia/escala** + benchmarks (4) con observabilidad (5).
5. **UX 1-comando** (6) y capa BSV de procedencia/incentivos (7).

## 9. Registro de avances
- 2026-11-12: plan creado (`docs/PLAN_VANGUARDIA.md`). En marcha **PRIORIDAD 1 (visual)**.
- 2026-11-12 (visual, web pública): rediseño de `web/public/index.html` (estado en vivo, topología agregada animada, hero/branding BSV+bitbeam, métricas de última ronda, progreso, fallback elegante sin coordinador) y **deploy** a `https://didiloco.bsv.direct` (`/var/www/didiloco`).
- 2026-11-12 (visual, dashboard/landing): actualización de `web/index.html` (hero/estado actual, CTA a console/estado público, copy BSV+bitbeam) y pulido de `web/dashboard.css` (limpieza de artefactos, estilo más consistente/premium); **deploy** de archivos web al VPS.
- 2026-11-12 (visual, console): mejoras en `web/dashboard.*` — launcher con formularios configurables y persistentes (coordinador/worker, identidad/invite/P2P), tabla de procesos con log/estado y mejor manejo de errores del agente, autoajuste de Agent URL y persistencia, mensajes claros ante 401/403/conexión fallida, metadatos de run/config en panel de rondas; **deploy** de archivos web al VPS.
- 2026-11-12 (visual, network): mejoras en `web/network.*` (topología más clara, metadatos run/config, manejo de errores 401/403/conexión, empty states, estilo pulido); **deploy** de archivos web al VPS.
- 2026-11-12 (P2.1, WAN): base de descarga de estado reanudable/verificada por HTTP — cache de blob y metadatos `state_blob_hash/state_blob_bytes` en `state_meta`, soporte `Range`/`206`/`416` en `/v1/state` (coordinador), cliente worker con descarga por chunks, reintentos/resume y verificación de hash; tests 39 OK.
- 2026-11-12 (P2.1, WAN): manifiesto por chunks (SHA256 por bloque + Merkle root), cabeceras de idempotencia/versión (`ETag`, `X-DiLoCo-*`, `If-None-Match`→304), verificación por chunk en worker y detección de cambio de estado durante descarga; pruebas de manifest/range/resume/reensamblado/416; tests 41 OK.
- 2026-11-12 (P2.1, WAN): idempotencia cliente — worker reutiliza estado en memoria si `state_hash` remoto coincide (evita re-descarga), en HTTP y gRPC (si hay `state_meta`); tests 41 OK.
- 2026-11-12 (P2.2, robustez): reintentos/backoff y recuperación idempotente en submit HTTP (transitorios, re-registro, duplicados/espera de resultado), reintentos en HTTP idempotente para 502/503/504; sidecar `tunnel-client` con reconexión automática/backoff; tests 41 OK.
- 2026-11-12 (web): `og:image` y metadatos sociales para web pública (`didiloco.bsv.direct`) y dashboard/consola/network; deploy web.
- 2026-11-12 (web/deploy): página HTML `503.html` (auto-retry) publicada en `/var/www/didiloco` y plantillas nginx actualizadas para mostrar fallback HTML en 502/503/504 durante reinicios de contenedores/proxy (dashboard) manteniendo JSON en `/api/status`. Nota: aplicar confs nginx en VPS requiere instalar en `user_conf.d` y recargar nginx.
- 2026-11-13 (P2.1, WAN): cache persistente local de blobs de estado por `state_hash` en worker (`runs/state-cache` / `--state-cache-dir`) para evitar re-descargas tras reinicios, con verificación de manifiesto/hash; prueba `test_worker_persistent_state_cache`; tests 42 OK.
- 2026-11-12 (infra reciente): `/v1/public/status`, deploy `didiloco.bsv.direct` (`/var/www/didiloco`, `proxy-net`), auth HTTP Basic/token en dashboard/agente, fixes de deploy y runtime Docker (numpy).
