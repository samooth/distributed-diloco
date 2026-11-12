# Deploy en VPS con Docker + `jonasal/nginx-certbot`

Configuración lista para integrarse con un nginx existente como este:

```yaml
services:
  nginx:
    image: jonasal/nginx-certbot:latest
    restart: unless-stopped
    environment:
      - CERTBOT_EMAIL=tomas@bsv.direct
    env_file:
      - ./nginx-certbot.env
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - /var/www/letsencrypt:/var/www/letsencrypt
      - /etc/letsencrypt:/etc/letsencrypt
      - ./user_conf.d:/etc/nginx/user_conf.d
      - /var/www:/var/www
      - ./logs:/var/log/nginx
      - ./nginx-gw.conf:/etc/nginx/user_conf.d/gw.conf:ro
    networks:
      - proxy-net

networks:
  proxy-net:
    external: true
```

## Caso configurado: `didiloco.bsv.direct` en `space.bsv.direct`

- **VPS destino**: `space.bsv.direct`
- **Código/compose en VPS**: `/opt/didiloco`
- **Web pública (host, montada en nginx)**: `/var/www/didiloco`
- **Dominio**: `didiloco.bsv.direct`
- **Vhost nginx**: `deploy/nginx/didiloco.bsv.direct.conf` (sirve estático desde `/var/www/didiloco`, proxyea `/api/status` a `diloco-coordinator:8095`; usa `resolver 127.0.0.11` + `proxy_pass` con variable para no romper nginx si los contenedores no están arriba)
- **Contenedores DiLoCo**: `diloco-coordinator` y `diloco-agent` en la red externa `proxy-net` (`deploy/docker-compose.diloco.yml`)

> Nota: el vhost incluido preserva las rutas de certificado que indicaste (`/etc/letsencrypt/live/didiloco.direct/...`). Verifica que correspondan al certificado emitido para `didiloco.bsv.direct`. Si certbot genera el cert en `/etc/letsencrypt/live/didiloco.bsv.direct/`, ajusta las rutas `ssl_certificate*` en el conf.

## Archivos importantes

- `deploy/nginx/ddiloco.bsv.direct.conf` → copiar a tu `user_conf.d/` del nginx-certbot
- `deploy/nginx/dash.ddiloco.bsv.direct.conf` → copiar a tu `user_conf.d/` del nginx-certbot
- `web/public/` → copiar a `/var/www/ddiloco.bsv.direct/` en el host del VPS (nginx ya monta `/var/www`)
- `deploy/docker-compose.diloco.yml` → compose de servicios DiLoCo (usa `proxy-net` externo)
- `deploy/.env.example` → copiar a `deploy/.env` en el VPS y ajustar `AGENT_TOKEN`
- `deploy/deploy_vps.sh` → script de publicación/actualización remota
- `configs/docker-coordinator.json` → config del coordinador para Docker (`host: 0.0.0.0`, puerto 8095)

## DNS

Crear registro A/AAAA:
- `didiloco.bsv.direct` → IP del VPS (`space.bsv.direct`)

`jonasal/nginx-certbot` solicitará/renovará certificados automáticamente según el `server_name` del vhost en `user_conf.d`.

## Deploy automático (recomendado)

SSH ya configurado para `space.bsv.direct`. Desde tu máquina local:

```bash
# Defaults del script: target space.bsv.direct, path /opt/didiloco, web /var/www/didiloco
chmod +x deploy/deploy_vps.sh

NGINX_USER_CONF_DIR=/ruta/en/vps/al/user_conf.d \
NGINX_CONF_FILES="didiloco.bsv.direct.conf" \
NGINX_CONTAINER=<nombre-contenedor-nginx> \
./deploy/deploy_vps.sh
```

Ejemplo explícito:

```bash
NGINX_USER_CONF_DIR=/opt/nginx/user_conf.d \
NGINX_CONF_FILES="didiloco.bsv.direct.conf" \
NGINX_CONTAINER=nginx \
./deploy/deploy_vps.sh space.bsv.direct:/opt/didiloco
```

Notas:
- `NGINX_USER_CONF_DIR` = ruta **host** montada como `./user_conf.d:/etc/nginx/user_conf.d` en tu compose de nginx.
- `NGINX_CONF_FILES` vacío => no instala/reemplaza vhosts (solo publica `web/public`), pero deja `deploy/nginx/didiloco.bsv.direct.conf` en `/opt/didiloco/deploy/nginx/` para revisarlo.
- `NGINX_CONTAINER` permite `nginx -t` + reload automático (`docker ps` para identificarlo).
- No sobrescribe `deploy/.env` remoto. Variables útiles: `DRY_RUN=1`, `SKIP_BUILD=1`, `SKIP_NGINX=1`, `SKIP_CONTAINERS=1`.

## Deploy manual (paso a paso en VPS)

### 1) Página pública y vhost nginx

```bash
# Web pública (montada en nginx como /var/www)
sudo mkdir -p /var/www/didiloco
sudo cp -r /opt/didiloco/web/public/* /var/www/didiloco/

# Vhost (ajusta la ruta host real de tu user_conf.d)
sudo cp /opt/didiloco/deploy/nginx/didiloco.bsv.direct.conf /ruta/en/vps/al/user_conf.d/
# Revisa ssl_certificate* si tu cert para didiloco.bsv.direct está en otra ruta/nombre
```

### 2) Contenedores DiLoCo (`proxy-net`)

```bash
cd /opt/didiloco
cp -n deploy/.env.example deploy/.env
# edita deploy/.env -> AGENT_TOKEN fuerte

docker network create proxy-net || true
docker compose -f deploy/docker-compose.diloco.yml --env-file deploy/.env up -d --build
docker compose -f deploy/docker-compose.diloco.yml ps
```

### 3) Recargar nginx-certbot

```bash
docker exec <nginx-container> nginx -t
docker exec <nginx-container> nginx -s reload
```

## Verificación

```bash
curl -sS https://didiloco.bsv.direct/api/status | jq .
curl -I https://didiloco.bsv.direct
```

En el VPS:

```bash
cd /opt/didiloco
docker compose -f deploy/docker-compose.diloco.yml logs -f --tail=100
```

## Seguridad (importante)

- **No expongas** `8095/8765` públicamente. Solo `80/443` vía nginx.
- **Nunca** publiques `/v1/register`, `/v1/update`, `/v1/state`, `/launch/*`, `/stop`. El vhost solo proxyea `/api/status` → `/v1/public/status`.
- Cambia `configs/docker-coordinator.json` (`coordinator.token`) y `deploy/.env` (`AGENT_TOKEN`).
- Si habilitas `/dashboard/` en el vhost (privado, proxyeado a `diloco-agent`), protégelo con `auth_basic` y/o allowlist/VPN antes de exponerlo.
- El vhost usa `resolver 127.0.0.11` + `proxy_pass` con variable para que nginx pueda arrancar/recargar aunque los contenedores DiLoCo no estén activos (la web pública seguirá sirviendo; `/api/status` devolverá 503 JSON).

## Problemas comunes

- **502/503 en `/api/status`**: `diloco-coordinator` caído o fuera de `proxy-net`. Revisa `docker compose ... ps/logs` y que ambos stacks usen `proxy-net`.
- **Nginx no recarga por conflicto de `server_name`**: ya tenías un vhost para `didiloco.bsv.direct`; instala `didiloco.bsv.direct.conf` reemplazando/ajustando el existente (no dupliques el mismo dominio). El script solo lo instala si pasas `NGINX_CONF_FILES`.
- **Certificado no coincide**: verifica rutas `ssl_certificate*` en el vhost contra el cert real emitido por certbot para el dominio.
- **Cambios en `web/public` no se ven**: re-copia a `/var/www/didiloco` o reejecuta deploy.
- **Coordinador inaccesible desde nginx**: `coordinator.host` debe ser `0.0.0.0` en `configs/docker-coordinator.json`.
