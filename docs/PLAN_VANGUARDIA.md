# Plan de vanguardia — DiLoCo WAN (BSV + bitbeam)

Plan vivo para convertir `distributed-diloco` en un sistema distribuido de entrenamiento **robusto, verificable y con UX de clase mundial** para LoRA/QLoRA 32B–70B sobre WAN.

**Regla operativa:** avanzar en orden de prioridad, y tras cada bloque de progreso relevante **desplegar/actualizar** (`NGINX_CONF_FILES="" NGINX_CONTAINER="" ./deploy/deploy_vps.sh` para publicar código/web + contenedores; usar `deploy/deploy.sh` si solo hace falta rsync+compose).

**Estado base (referencia):** tests `39 OK`, coordinador/worker HTTP+gRPC opcional, compresión int8/top-k+EF, churn/rejoin, túnel bitbeam v4, identidad BSV MVP (admisión/firma), invite firmado, `/v1/public/status`, web pública en `didiloco.bsv.direct`, dashboard agente con auth HTTP Basic/token.

Leyenda: `[x]` hecho · `[~]` en progreso/parcial · `[ ]` pendiente

---

## 1. Visual y experiencia de producto (PRIORIDAD 1) — completado
Objetivo: que el proyecto se perciba “kickass” desde el primer contacto: estado público, dashboard operativo y narrativa técnica claros, modernos y confiables.

### 1.1 Web pública de estado (`web/public/index.html`, `didiloco.bsv.direct`)
- [x] Endpoint sanitizado `/v1/public/status` + fallback resiliente en nginx (ya operativo).
- [x] Rediseño visual premium: hero/identidad BSV+bitbeam, layout claro, animaciones sutiles, tipografía y jerarquía.
- [x] Visualización de estado más rica: progreso, métricas clave, última ronda, estado de red, indicadores en vivo, manejo elegante de “sin datos”.
- [x] Histórico agregado público (sparklines/tendencias) cuando el endpoint exponga series; degradación elegante si no hay datos (`public_status.history` sanitizado, últimas rondas).
- [x] Secciones informativas: cómo funciona DiLoCo, stack (bitbeam/BSV, LoRA), seguridad/privacidad, CTA para unirse/contribuir (según política).
- [x] Accesibilidad, responsive, performance (sin dependencias pesadas innecesarias): landmarks/skip-link, ARIA/progressbar, focus visible, responsive; i18n es/en opcional pendiente.

### 1.2 Dashboard/console de agente (`web/dashboard.*`, `web/index.html`, `web/network.*`)
- [x] Pulido visual y consistencia (tema, componentes, tablas, empty states, toasts, charts).
- [x] UX de conexión/operación: wizard claro, estados de auth/agente, feedback de errores, prefill seguro, separación coordinador vs agente.
- [x] Launcher seguro y usable: formularios/perfiles (no prompts hardcodeados), validación, logs/estado de procesos en vivo (tail vía agente), confirmaciones.
- [x] Vistas de red/topología y salud (workers, rondas, latencia/sync) con datos reales disponibles; métricas/estado enriquecidos según lo expuesto por el coordinador.
- [x] Observabilidad en UI: eventos/logs, historial/timeline de rondas, export/reportes (JSON/CSV), modo oscuro/claro consistente.

### 1.3 Marca, docs visuales y narrativa
- [x] README/landing (`web/index.html`) alineados: demo visual, arquitectura, resultados, quickstart real.
- [x] Assets ligeros (diagrama SVG `web/assets/architecture.svg`) y guía de despliegue pública/privada clara (`docs/DEPLOY_PUBLIC_DASHBOARD.md`, `README.md`, `deploy/README.md`).

**Criterio de avance 1:** web pública y dashboard se ven profesionales en desktop/móvil, comunican estado real y no exponen datos sensibles. **Deploy tras cada mejora visible.** (completado)

---

## 2. Robustez WAN y transporte (PRIORIDAD 2)
Objetivo: entrenamiento estable sobre internet real (NAT, cortes, latencia variable).

- [x] **Estado chunked + reanudable + verificado**: `/v1/state` con HTTP Range/reanudación, cache de blob en coordinador y metadatos en `state_meta` (`state_blob_hash/state_blob_bytes`, `state_blob_chunk_size`, `state_blob_chunk_hashes`, `state_blob_merkle_root`, `state_etag`). Worker: descarga HTTP por chunks alineados al manifiesto, reintentos/resume, detección de cambio de estado durante descarga, verificación por chunk + hash/Merkle/blob, **reuso en memoria** y **cache persistente local por `state_hash`** (`runs/state-cache` o `--state-cache-dir`) para evitar re-descargas tras reinicios. Coordinador expone `ETag`/`X-DiLoCo-{Round,State-Hash,Blob-Hash}` y `If-None-Match` (304). **gRPC (paridad estado/bootstrap):** `GetStateMeta`/`GetStateChunk` con manifiesto y chunking, descarga verificada (chunk/hash/blob), reanudación/reintentos desde offset, detección de cambio de estado a mitad de descarga y fallback a `GetState` completo para compatibilidad con servidores antiguos; worker reutiliza cache en memoria/disco también en gRPC. **Checkpoints/artefactos grandes:** `/v1/checkpoint_meta` (manifiesto por archivo con size/sha256/chunk_hashes/Merkle) y `/v1/checkpoint_file` (Range + ETag/If-None-Match + compresión wire negociada sobre rangos preservando offsets), con `diloco/checkpoint_transfer.py` (resume desde cache persistente por round/state_hash, verificación por chunk+archivo, reintentos, descompresión transparente). Pruebas HTTP y gRPC de manifest/chunk/reensamblado/resume/cache + pruebas de checkpoints (verificación, resume, cache corrupta, compresión, rangos/ETag/errores). Pendiente: paridad de transferencia de checkpoints en gRPC y validación con artefactos 32B/70B reales.
- [~] **Compresión negociada**: `zstd`/codecs para bootstrap y deltas; medir bytes on-wire y `sync_fraction`. HTTP y gRPC: negociación por capacidades/config (`coordinator.wire_compression`: none/gzip/zstd/auto), compresión de bootstrap/estado (incl. chunks/rangos preservando offsets y verificación de manifiesto sobre bytes originales) y de submits/respuestas de updates, descompresión transparente en worker, métricas bytes-on-wire (`upload_bytes`/`state_response_bytes`) y tamaños sin comprimir en registros. El coordinador agrega `update_blob_bytes_total`/`update_blob_bytes_by_node` (bytes sin comprimir) junto a los bytes wire, expuestos en métricas, `status`, `public_status` e historial; UI pública/dashboard/network y CSV muestran el ahorro `(↓X%)` y el tamaño raw. Soporte `zstd` cuando el backend está disponible (gzip disponible por defecto). Pendiente: evaluación de compresión efectiva en escenarios WAN reales y agregados/tendencias del ratio en UI.
- [~] **Reconexión automática E2E**: worker con reintentos/backoff en submit (errores transitorios 0/502/503/504), recuperación de updates duplicados ambiguos, re-registro ante pérdida de sesión, espera idempotente de avance de ronda y **re-registro automático en heartbeat ante 403** (coordinador reiniciado/sesión perdida); sidecar `tunnel-client` bitbeam con reconexión automática/backoff manteniendo el listener local. **Hardening P2.3**: tests de re-registro tras pérdida de sesión, submit ambiguo y **reinicio real del coordinador reutilizando puerto**; el coordinador ahora sobreescribe checkpoints obsoletos al reiniciar en vez de fallar el run. Procedimiento multi-máquina en `docs/VALIDACION_WAN_NAT.md`. Pendiente: validación E2E WAN/NAT real y re-discovery dinámica automatizada.
- [ ] **Validación NAT/Internet**: E2E multi-máquina (topic y túnel por par), métricas de conexión (TTFB, reconexiones, fallos DHT), fallback coordinado.
- [~] **Túnel identidad dinámico**: `tunnel-server-id` (bitbeam) soporta **alta/late join dinámica** de peers releyendo `--peer-pubkey-file` periódicamente y añadiendo listeners DHT para nuevos pubkeys sin reiniciar, y **baja/revocación en caliente** (cierra listener y derriba conexiones activas del peer revocado). `run_coordinator.py` exporta la allowlist efectiva al peer file de forma **atómica/idempotente** vía `diloco/tunnel_allowlist.py` (normalización validada, orden canónico, helpers de reconciliación), con tests unitarios. Pendiente: validación E2E NAT/multi-máquina y actualización de allowlist en tiempo de ejecución (hot-reload del config del coordinador).
- [~] **Manejo de stragglers**: reporting claro mejorado (estado interno/público con `waiting_nodes`/`waiting_workers`, tiempo de espera de ronda, visibilidad en dashboard/network/pública y diagnósticos de timeout con nodos en espera). Timeout de ronda **adaptativo** opcional (`round_timeout_adaptive`, EMA sobre tiempos observados con límites min/max) y **políticas de ronda parcial** seguras opcionales (`allow_partial_round`, mínimo de updates, conservadoras por defecto/desactivadas), con métricas/estado (`partial`, `updates_received`, timeout efectivo/EMA) expuestos. Pendiente: ajuste/evaluación de políticas en WAN real, interacción con churn/rejoin y endurecimiento bajo stragglers prolongados.
- [x] **Suite de resiliencia**: `tests/test_resilience.py` inyecta latencia, pérdida transitoria, ráfagas en submit y churn (muerte/rejoin con cache persistente) verificando recuperación ≤2 rondas sin corrupción de estado; `scripts/run_resilience_wan.py` ejecuta un run E2E a través de `scripts/wan_emulate.py` (delay/jitter/loss/banda) con churn+rejoin; `scripts/resilience_netem.sh` añade `tc netem` opcional sobre loopback (con fallback sin root/tc). Pendiente: ejecutar la variante `tc netem` en un entorno con privilegios y validar en enlaces WAN reales.

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
1. **Visual/producto** (1.1 → 1.2 → 1.3), con deploys frecuentes.  ← *completado (P1)*
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
- 2026-11-13 (P2.2, robustez): worker intenta re-registro automático en heartbeat ante 403 (recuperación tras reinicio/pérdida de registro del coordinador); tests 42 OK.
- 2026-11-13 (P2.2, transporte): `tunnel-server-id` añade listeners dinámicamente para nuevos peers desde `--peer-pubkey-file` (late join sin reinicio); dashboard/agent soportan `--state-cache-dir` en launcher worker. Deploy con rebuild.
- 2026-11-13 (P2.1, robustez): cache de estado endurecida por `state_blob_hash`/round (fallback compatible), validación de `round/global_step` en blobs cacheados, y tests de resume/stale/cache aislados de `runs/state-cache` ambiente; suite 43 OK. Commit `40fc2f4`, desplegado.
- 2026-11-13 (P2.1/P2, robustez+medición): prueba de reanudación ante cambio de estado/blob a mitad de descarga; métricas por ronda en worker (`state_response_bytes`, `submit_seconds`, `sync_fraction`, instrumentación de fetch de estado). `tunnel-server-id` soporta revocación/baja en caliente reconciliando `--peer-pubkey-file` (preserva peers estáticos). Suite 44 OK. Commit `450c698`, desplegado.
- 2026-11-13 (P2.2/P2, transporte+compresión, en curso): `run_coordinator.py` exporta allowlist efectiva a archivo (`checkpoint_dir/allowed_peer_pubkeys.txt`) y lo pasa al túnel de identidad para facilitar late-join/revocación por actualización de archivo; base de soporte opcional `zstd` en compresión/config (disponibilidad por backend). Pendiente negociación end-to-end y compresión efectiva de payloads.
- 2026-11-13 (P2, medición bytes/sync): coordinador registra bytes recibidos por update y métricas de ronda (`update_bytes_total`, `update_bytes_by_node`, `state_blob_bytes`); worker agrega totales en resumen (`total_train/submit_seconds`, `total_upload/state_response_bytes`, `avg_sync_fraction`). Tests 44 OK.
- 2026-11-13 (P2, UI/observabilidad): métricas de bytes/sync expuestas en `public_status` (última ronda) y visualizadas en web pública, dashboard y network (upload total/state blob por ronda), con fallback cuando no hay datos. Tests 44 OK.
- 2026-11-13 (P1.1, web pública): `public_status` expone historial agregado sanitizado (`history`, últimas 24 rondas, sin IDs/claves/logs) para tendencias; `web/public/index.html` actualizado con sparklines/tendencias (loss, delta norm, sync, upload) con degradación elegante, secciones informativas ampliadas (cómo funciona, stack bitbeam/BSV+LoRA, seguridad/privacidad, unirse/contribuir según política) y mejoras de accesibilidad/responsive/performance (skip-link, landmarks, ARIA/progressbar, focus visible). Tests 44 OK. Commit `08aea07`, desplegado.
- 2026-11-13 (P1.2, dashboard/console/network): mejoras UX/operación y observabilidad — wizard y mensajes con separación coordinador/agente, estados/auth/errores de agente, prefill seguro; launcher con validación y confirmaciones; visor de logs de procesos en vivo (`GET /process/log` en agente, protegido) y estado de procesos; network monitor enriquecido (salud/sync/metadatos de run); export JSON/CSV, eventos/logs, historial de rondas y consistencia visual/tema. Tests 44 OK. Commit `cee0c45`.
- 2026-11-13 (P1.3, marca/docs/narrativa): README y landing (`web/index.html`) alineados (estado actual, arquitectura, quickstart, despliegue público/privado), diagrama SVG ligero (`web/assets/architecture.svg`) y guía de despliegue pública/privada clarificada (`docs/DEPLOY_PUBLIC_DASHBOARD.md`). **P1 completado.** Tests 44 OK. Commit `8b65bbd`.
- 2026-11-14 (P2, stragglers/reporting): coordinador expone estado de espera de ronda (`waiting_nodes`/`waiting_workers`, `round_wait_seconds`, flags/timeouts) en status/health/público; timeouts de ronda incluyen nodos en espera en diagnóstico; dashboard, network y web pública muestran recibidos/esperando y tiempo de espera. Tests 44 OK. Commit `c4a7be5`, desplegado.
- 2026-11-14 (P2.1, gRPC state parity): proto/transport extendidos con `GetStateMeta` y `GetStateChunk` (más campos de identidad en `RegisterRequest`), regeneración de stubs; coordinador sirve metadatos y chunks validados por nodo; cliente/worker gRPC usa descarga chunked con verificación de manifiesto/hash/blob, reintentos/reanudación desde offset, detección de cambio de estado a mitad de descarga, reuso de cache memoria/disco y fallback a `GetState` completo para compatibilidad. Tests 47 OK. Commit `457de3d`, desplegado.
- 2026-11-14 (P2.2, wire compression HTTP): compresión negociada a nivel de transporte para HTTP (`coordinator.wire_compression` none/gzip/zstd/auto, gzip disponible), aplicada a bootstrap/estado (incl. rangos/chunks preservando offsets y verificación de manifiesto) y submits de deltas con descompresión transparente; capacidades anunciadas en register/state_meta/status; métricas bytes-on-wire (`upload_bytes`/`state_response_bytes`) y tamaños sin comprimir en registros. Tests 48 OK. Commit `81968bd`, desplegado.
- 2026-11-16 (P2.2, wire compression gRPC): paridad de compresión negociada en transporte gRPC (metadatos de aceptación/codec, compresión/descompresión de `GetState`/`GetStateChunk`/`Submit` request+response, tracking de bytes-on-wire en cliente/worker) compatible con fallback sin compresión; test de roundtrip gRPC con gzip. Tests 49 OK. Commit `88c5aee`, desplegado.
- 2026-11-16 (P2.6, stragglers): timeout de ronda adaptativo opcional (EMA sobre `sync_seconds` observados, límites min/max) y finalización parcial segura opcional al expirar timeout (`allow_partial_round`, `partial_round_min_updates`, conservadora/desactivada por defecto), con campos de métricas/estado (`partial`, `updates_received`, `effective_round_timeout_s`, EMA) en status/health/público/historial. Tests 52 OK. Commit `a440b59`, desplegado.
- 2026-11-17 (P2.2, reporting de compresión): el coordinador registra `update_blob_bytes_total`/`update_blob_bytes_by_node` (bytes sin comprimir) junto a bytes wire (HTTP+gRPC), expuestos en métricas/status/public_status/historial; UI pública, dashboard y network muestran ahorro `(↓X%)` + tamaño raw y CSV exporta la nueva columna. Tests 53 OK. Commit `e9e7e41`, desplegado.
- 2026-11-17 (P2.7, suite de resiliencia): `tests/test_resilience.py` con 4 pruebas (latencia+pérdida periódica, ráfaga transitoria en submit, churn+rejoin con cache, pérdida en descarga de estado con verificación de hash); `scripts/run_resilience_wan.py` E2E a través del proxy WAN con churn+rejoin; `scripts/resilience_netem.sh` con `tc netem` opcional. Suite 57 OK (53 + 4) + E2E resilience=OK. Commit `78e57b9`, desplegado.
- 2026-11-17 (P2.1, checkpoints chunked): `/v1/checkpoint_meta` y `/v1/checkpoint_file` en el coordinador (manifiesto size/sha256/chunk_hashes/Merkle por archivo, Range, ETag/If-None-Match, compresión wire negociada sobre rangos), plus `diloco/checkpoint_transfer.py` (descarga reanudable desde cache persistente, verificación chunk+archivo, reintentos, descompresión). 6 tests nuevos (verificación, resume, cache corrupta, gzip, rangos/304/416/400, auth). Suite 63 OK. Commit `a5e9bf9`, desplegado.
- 2026-11-17 (P2.5, allowlist↔túnel): `diloco/tunnel_allowlist.py` con export atómico/idempotente y validado de la allowlist al peer file (usado por `run_coordinator.py`) + helpers de reconciliación; `tunnel-server-id` ahora derriba conexiones activas al revocar un peer. 5 tests nuevos. Suite 68 OK. Commit `e056271`, desplegado.
- 2026-11-17 (P2.3, reconexión): 3 tests de re-registro tras sesión perdida, submit ambiguo y reinicio real del coordinador en el mismo puerto; fix: el coordinador sobreescribe checkpoints obsoletos tras reinicio en lugar de fallar. Suite 71 OK. Commit `1e1fda6`, desplegado.
- 2026-11-12 (infra reciente): `/v1/public/status`, deploy `didiloco.bsv.direct` (`/var/www/didiloco`, `proxy-net`), auth HTTP Basic/token en dashboard/agente, fixes de deploy y runtime Docker (numpy).
