# Despliegue: página pública + dashboard privado (DiLoCo WAN)

Guía para exponer una **página pública de solo lectura** y mantener el **plano de control privado** (coordinador, dashboard/agente) de forma segura.

## 1. Objetivo y separación de planos

- **Público (solo lectura):** landing/estado de la red con métricas **agregadas y sanitizadas** (ronda, workers activos, progreso, última ronda, tendencias agregadas). Sin IDs de nodos, pubkeys/WIF, endpoints de control, logs, rutas ni hashes sensibles.
- **Privado (control/operación):** `web/dashboard.*`, `web/network.*` y `scripts/agent.py` (launcher de procesos), además del coordinador (`/v1/*` interno). **Nunca exponerlo abiertamente a internet**: requiere autenticación y aislamiento de red (VPN/allowlist/proxy seguro).

**Despliegue de referencia actual:** dominio público `https://didiloco.bsv.direct` (web en `/var/www/didiloco`, `/api/status` → `/v1/public/status`) sobre VPS con nginx + Docker (`proxy-net`).

## 2. Componentes

- **Coordinador** (`scripts/run_coordinator.py`, HTTP 8095 por defecto): expone `/v1/health`, `/v1/status`, `/v1/state`, `/v1/update`, etc. y el endpoint público sanitizado **`GET /v1/public/status`** (no requiere token). Los demás endpoints deben permanecer privados/protegidos.
- **Agente** (`scripts/agent.py`, 8765 por defecto): sirve la web y permite lanzar/detener procesos y ver logs de procesos gestionados (`/processes`, `/launch/*`, `/stop`, `/process/log`). **Es capacidad de ejecución remota**: tratar como altamente sensible.
- **Web pública** (`web/public/index.html`): consume `/api/status` (o `/v1/public/status`) con auto-refresh y degradación elegante cuando no hay datos.
- **Nginx/proxy**: sirve estáticos públicos y proxyea solo la ruta pública sanitizada; opcionalmente proxyea dashboard privado detrás de auth fuerte.

## 3. Seguridad (obligatoria)

- **Menor exposición:** no publicar puertos `8095`/`8765` en internet. Solo `80/443` vía proxy.
- **Proxy público estricto:** exponer únicamente `GET /api/status` (o `/v1/public/status`). **Nunca** proxyear `/v1/register`, `/v1/update`, `/v1/state`, `/launch/*`, `/stop`, `/processes`, `/process/log`.
- **Autenticación del agente:** ejecutar con `--token` (o `AGENT_TOKEN`). Cuando hay token, el agente protege UI y endpoints de control (`/health` permanece abierto para checks). Navegador: HTTP Basic (contraseña = `AGENT_TOKEN`). API: header `X-DiLoCo-Token` o `?token=`. **Añadir capas extra** en nginx (auth básica, allowlist IP, VPN/Tailscale, SSO/Access) si se expone.
- **Datos sanitizados:** `/v1/public/status` solo devuelve agregados (run/progreso/contadores/última ronda/métricas agregadas/historial agregado). Verificar ante cambios que no filtre identidad, claves, logs ni control.
- **Defensa en profundidad:** HTTPS, firewall, headers de seguridad, rate limiting en la ruta pública, rotación de tokens/secretos, revisión de logs.
- **Secretos:** no versionar `deploy/.env`, WIF/identidades (`runs/identity/*.json` contiene material sensible), tokens ni invites privados.

## 4. Arquitectura de red (referencia)

```text
Internet
  │
  └── didiloco.bsv.direct (443)
        ├─ /           -> estático público (/var/www/didiloco, web/public/*)
        └─ /api/status -> proxy a diloco-coordinator:8095/v1/public/status (solo lectura)

Red privada / proxy-net (Docker)
  ├── diloco-coordinator:8095  (no publicado al host/internet)
  └── diloco-agent:8765        (no publicado al host/internet; solo acceso autenticado/privado)
```

> El dashboard privado puede servirse en un subdominio/host interno protegido (p. ej. `dash.*`) o ruta autenticada, siempre apuntando al agente **solo desde red confiable** y con auth fuerte.

## 5. Deploy con Docker + nginx-certbot (recomendado en este repo)

Artefactos relevantes:
- `deploy/docker-compose.diloco.yml` — `diloco-coordinator` y `diloco-agent` en red externa `proxy-net`, sin puertos expuestos al host.
- `configs/docker-coordinator.json` — coordinador escuchando en `0.0.0.0:8095` dentro de Docker.
- `deploy/.env.example` — copiar a `deploy/.env` en VPS y definir `AGENT_TOKEN` fuerte (`openssl rand -hex 32`).
- `deploy/nginx/didiloco.bsv.direct.conf` — vhost público (estático + `/api/status` resiliente con fallback).
- `deploy/nginx/dash.ddiloco.bsv.direct.conf` — ejemplo de dashboard privado en subdominio (requiere protección adicional).
- `deploy/deploy_vps.sh` — rsync + publicación de `web/public` + compose up/build; **no sobrescribe** `deploy/.env` remoto.
- `deploy/README.md` — detalles de integración con `jonasal/nginx-certbot`.

### 5.1 Pasos típicos

1. **DNS:** crear A/AAAA de `didiloco.bsv.direct` al VPS.
2. **Firewall:** permitir solo `22,80,443`.
3. **Preparar VPS:**
   - Proyecto en `/opt/didiloco` (por defecto del script).
   - Red Docker externa: `docker network create proxy-net || true` (si no existe).
   - `cp -n deploy/.env.example deploy/.env` y configurar `AGENT_TOKEN` fuerte.
4. **Publicar/actualizar:**
   ```bash
   # desde el repo local (sync + web pública + rebuild contenedores; sin tocar nginx conf)
   NGINX_CONF_FILES="" NGINX_CONTAINER="" ./deploy/deploy_vps.sh
   ```
   Para instalar/refrescar vhost y recargar nginx, define `NGINX_USER_CONF_DIR`, `NGINX_CONF_FILES` y `NGINX_CONTAINER` (ver `deploy/README.md`).
5. **Nginx:** instalar/adaptar vhost en `user_conf.d` del stack nginx-certbot (montado en el contenedor). Verificar rutas de certificados `ssl_certificate*` para el dominio real.
6. **Verificar:**
   ```bash
   curl -sS https://didiloco.bsv.direct/api/status | jq .
   curl -I https://didiloco.bsv.direct
   ```
   En VPS: `docker compose -f deploy/docker-compose.diloco.yml --env-file deploy/.env ps/logs`.

## 6. Checklist de verificación

- [ ] DNS apunta al VPS y HTTPS válido (certbot/nginx).
- [ ] `8095` y `8765` no accesibles desde internet; solo `80/443` expuestos.
- [ ] `/api/status` devuelve JSON sanitizado (sin nodos/IDs/claves/logs/endpoints privados).
- [ ] No existen proxies públicos a `/v1/register`, `/v1/update`, `/v1/state`, `/launch/*`, `/stop`, `/processes`, `/process/log`.
- [ ] `AGENT_TOKEN` fuerte configurado en `deploy/.env`; acceso al dashboard/agente solo autenticado y restringido.
- [ ] `deploy/.env` y secretos/identidades no versionados ni expuestos.
- [ ] Web pública responde con fallback adecuado cuando el coordinador no está disponible.

## 7. Alternativas

- **Caddy:** ver `deploy/Caddyfile.ddiloco.example` (adaptar a dominio real y rutas públicas/privadas).
- **systemd:** ejemplos `deploy/diloco-coordinator.service.example` y `deploy/diloco-agent.service.example` (mantener binds privados y proxy seguro).
- **Acceso privado recomendado:** VPN (Tailscale/WireGuard) o SSO/Access en lugar de exponer dashboard a internet.

## 8. Problemas comunes

- **`/api/status` 502/503:** coordinador caído o fuera de `proxy-net`; revisar contenedores/red. El vhost público sirve fallback JSON para resiliencia.
- **Cambios en `web/public` no visibles:** republicar a `/var/www/didiloco` (re-ejecutar deploy).
- **Conflicto `server_name` en nginx:** no duplicar el mismo dominio en `user_conf.d`; instalar el vhost correcto.
- **Cert no coincide:** ajustar `ssl_certificate*` al cert emitido para el dominio.
- **401 en agente/dashboard:** token no enviado o incorrecto; para navegador usar HTTP Basic con contraseña `AGENT_TOKEN` (o configurar auth/capas según entorno).

## Referencias
- [deploy/README.md](../deploy/README.md)
- [docs/06-operaciones.md](06-operaciones.md)
- [docs/07-seguridad.md](07-seguridad.md)
- [docs/03-comunicacion.md](03-comunicacion.md)
- [docs/ONBOARDING.md](ONBOARDING.md)