# Despliegue: página pública + dashboard privado (ddiloco.bsv.direct)

Este documento describe cómo exponer una **página pública de solo lectura** y un **dashboard privado de control** para DiLoCo WAN usando el subdominio `ddiloco.bsv.direct`.

## 1. Objetivo

- **Público (`ddiloco.bsv.direct`)**: Landing + estado agregado de la red (ronda, workers activos, estado, últimas métricas). Sin IPs, IDs detallados, logs, claves o endpoints de control.
- **Privado (`dash.ddiloco.bsv.direct` o `/app`)**: Dashboard operativo existente (`web/dashboard.html`), agente (`scripts/agent.py`) para lanzar coordinador/worker, generación de invites/identidades, logs. Protegido por autenticación/VPN.

## 2. Principios de seguridad

- **Principio de menor exposición**: El coordinador (`/v1/*`) y el agente (`/launch/*`, `/stop`) **nunca** deben ser accesibles desde internet. Solo desde `127.0.0.1` o red privada (Tailscale/Netbird/WireGuard).
- **Solo datos sanitizados**: La API pública debe devolver únicamente campos agregados, sin `node_id`, pubkeys, WIF, rutas, logs completos ni información de identidad.
- **Autenticación obligatoria**: El panel privado debe ir detrás de auth (Basic Auth en proxy, Tailscale, Cloudflare Access, Authelia, etc.).
- **Defensa en profundidad**: Usar HTTPS, rate-limiting en la ruta pública, headers de seguridad, firewall (solo 80/443).
- **Token del agente**: Ejecutar `./agent --token <secreto> --host 127.0.0.1 --port 8765` (protege POST de control).

## 3. Arquitectura

```text
Internet
  │
  ├── ddiloco.bsv.direct/        -> Página pública estática + GET /api/status (sanitizado)
  └── dash.ddiloco.bsv.direct/   -> Dashboard privado (auth) -> proxy a 127.0.0.1:8765 (agente)

VPS (localhost)
  ├── 127.0.0.1:8095  -> Coordinador DiLoCo (/v1/* interno, con token)
  ├── 127.0.0.1:8765  -> Agente (web/ + /launch/*, /stop) con --token
  └── 127.0.0.1:80/443 -> Caddy/Nginx (reverse proxy)
```

## 4. Cambios necesarios en el repo

### 4.1 Endpoint público sanitizado (Coordinador)
Añadir ruta **sin autenticación** con datos agregados:

- Ruta propuesta: `GET /v1/public/status` (o `GET /public/status`)
- Campos sanitizados: `run_id`, `round`, `max_rounds`, `global_step`, `finished`, `failure` (booleano/estado), `workers_active` (conteo), `quorum`, `pending_updates`, `last_result_summary` (pérdida/round sin detalles por nodo), `uptime/last_activity` (opcional), `config_hash` (opcional, seguro)
- **Nunca** exponer: `registered` (lista), `pending_updates` (lista detallada), `history` completo, pubkeys, node_ids, logs, rutas de checkpoints.

Implementación en `diloco/coordinator.py` (handler HTTP): añadir ruta antes de `self._authorize()` para `/v1/public/status`.

### 4.2 Página pública
Crear `web/public/` con:
- `index.html` - Landing + estado en tiempo real (fetch a `/api/status` o `/v1/public/status`)
- `styles.css` / `app.js` - Diseño minimalista, auto-refresh (cada 10-30s)
- Datos mostrados: estado, ronda/progreso, workers activos, última ronda, enlace a dashboard privado (oculto o discreto)

### 4.3 Agente / despliegue
- Ejecutar agente solo en localhost: `./agent --host 127.0.0.1 --port 8765 --token $(cat /etc/diloco/agent.token)`
- Servir `web/` existente para el dashboard privado (ya lo hace)

## 5. Despliegue con Docker + nginx existente (jonasal/nginx-certbot)

Si ya tienes nginx con `jonasal/nginx-certbot` y red externa `proxy-net`, usa los artefactos listos en `deploy/`:

- `deploy/docker-compose.diloco.yml`: servicios `diloco-coordinator` y `diloco-agent` en `proxy-net` (externa), sin exponer puertos al host.
- `deploy/nginx/ddiloco.bsv.direct.conf` y `deploy/nginx/dash.ddiloco.bsv.direct.conf`: vhosts compatibles con `user_conf.d` de nginx-certbot.
- `configs/docker-coordinator.json`: config con `host: 0.0.0.0` y puerto `8095` para red Docker.
- `deploy/deploy_vps.sh`: sincroniza código/configs, publica `web/public/`, copia vhosts, levanta/actualiza contenedores y recarga nginx.
- `deploy/README.md`: guía específica de integración.

Ejemplo de despliegue:

```bash
NGINX_USER_CONF_DIR=/ruta/en/vps/nginx/user_conf.d \
PUBLIC_WWW_DIR=/var/www/ddiloco.bsv.direct \
NGINX_CONTAINER=<contenedor-nginx> \
./deploy/deploy_vps.sh user@vps.tls:/opt/ddiloco
```

## 6. Configuración del proxy (Caddy alternativo)

Archivo `/etc/caddy/Caddyfile`:

```caddy
# Página pública
ddiloco.bsv.direct {
    encode gzip zstd
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
        X-Content-Type-Options nosniff
        X-Frame-Options SAMEORIGIN
        Referrer-Policy strict-origin-when-cross-origin
        -Server
    }
    
    # Servir página pública estática
    root * /home/tuuser/distributed-diloco/web/public
    try_files {path} /index.html
    file_server
    
    # Proxy del endpoint público sanitizado al coordinador
    @publicapi path /api/status /api/public/*
    handle @publicapi {
        uri replace /api/status /v1/public/status
        uri replace /api/public /v1/public
        reverse_proxy 127.0.0.1:8095
    }
    
    # Rate limit básico (opcional pero recomendado)
    # rate_limit { ... }
}

# Dashboard privado
dash.ddiloco.bsv.direct {
    encode gzip zstd
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
        X-Content-Type-Options nosniff
        X-Frame-Options SAMEORIGIN
        Referrer-Policy strict-origin-when-cross-origin
        -Server
    }
    
    # Autenticación básica (o reemplazar por Tailscale/Cloudflare Access)
    basicauth {
        admin $2a$14$... # generar con: caddy hash-password
    }
    
    # Proxy al agente (dashboard + control)
    reverse_proxy 127.0.0.1:8765 {
        header_up X-Forwarded-Proto {scheme}
        header_up X-Forwarded-Host {host}
    }
}
```

> Si prefieres usar **únicamente** `ddiloco.bsv.direct` (un solo subdominio), puedes poner la página pública en `/` y el dashboard privado en `/app/` con auth, ajustando las rutas del agente/web según convenga.

### Nginx alternativo (resumen)
Similar con `location /` (estático + proxy público), `location /app/` con `auth_basic` hacia `127.0.0.1:8765`. Caddy es más directo para HTTPS automático con Let's Encrypt.

## 7. Pasos de despliegue

1. **DNS**: Crear registros A/AAAA para `ddiloco.bsv.direct` y `dash.ddiloco.bsv.direct` apuntando al VPS.
2. **Firewall**: Permitir solo 22, 80, 443. Bloquear puertos del coordinador/agente desde internet.
3. **Repositorio**: Ya incluye endpoint público `GET /v1/public/status`, página `web/public/`, ejemplos en `deploy/` y compose para `proxy-net`.
4. **Proxy**:
   - **nginx-certbot (recomendado si ya lo usas)**: copiar `deploy/nginx/*.conf` a tu `user_conf.d` y publicar `web/public/` en `/var/www/ddiloco.bsv.direct`; o ejecutar `deploy/deploy_vps.sh`.
   - **Caddy alternativo**: adaptar `deploy/Caddyfile.ddiloco.example`.
5. **Servicios**:
   - **Docker**: `deploy/docker-compose.diloco.yml` (coordinador + agente en `proxy-net`).
   - **systemd alternativo**: `deploy/diloco-coordinator.service.example` y `deploy/diloco-agent.service.example`.
6. **Verificar**:
   - `curl https://ddiloco.bsv.direct/api/status` devuelve JSON sanitizado (sin auth).
   - `https://dash.ddiloco.bsv.direct` pide autenticación y sirve el dashboard privado.
   - Puertos 8095/8765 no accesibles desde internet.
7. **Operación**: Rotar tokens/contraseñas (`deploy/.env`, config del coordinador), revisar logs, usar invite/identidad según `docs/ONBOARDING.md`.

## 8. Checklist de seguridad

- [ ] Coordinador/agente no expuestos directamente a internet (solo localhost/red Docker interna)
- [ ] Endpoint público NO requiere token y devuelve solo datos agregados
- [ ] Dashboard privado protegido con auth/VPN (ideal Tailscale/Cloudflare Access)
- [ ] HTTPS habilitado (Let's Encrypt vía nginx-certbot/Caddy)
- [ ] Firewall restrictivo (solo 22,80,443)
- [ ] Agente con `--token` configurado (`deploy/.env` / systemd env)
- [ ] Sin exposición de `/v1/register`, `/v1/update`, `/v1/state`, `/launch/*` públicamente
- [ ] Rate limiting habilitado en ruta pública (si aplica)
- [ ] Headers de seguridad configurados

## 9. Notas

- Si el coordinador aún no está corriendo, la página pública mostrará "sin datos" correctamente.
- Para mayor seguridad, usar **Tailscale** para el dashboard privado en lugar de exponerlo a internet (incluso con auth básica).
- El endpoint público es intencionalmente limitado para evitar filtrar información operativa sensible.
- Con `jonasal/nginx-certbot`, los certificados se gestionan automáticamente a partir de los `server_name` en `user_conf.d`.
- `deploy/deploy_vps.sh` no sobrescribe `deploy/.env` remoto; revisa tokens tras el primer despliegue.

## Referencias
- [Operaciones](06-operaciones.md)
- [Seguridad](07-seguridad.md)
- [Comunicación](03-comunicacion.md)
- [Onboarding](ONBOARDING.md)
