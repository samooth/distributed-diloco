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

## Qué se despliega

- **`ddiloco.bsv.direct` (público)**: página estática `web/public/` + `GET /api/status` proxyeado a `http://diloco-coordinator:8095/v1/public/status` (datos sanitizados, sin auth).
- **`dash.ddiloco.bsv.direct` (privado)**: dashboard/agente proxyeado a `http://diloco-agent:8765` (proteger con auth/VPN).
- **Contenedores DiLoCo**: `diloco-coordinator` y `diloco-agent` en la red externa `proxy-net` (ver `docker-compose.diloco.yml`).

## Archivos importantes

- `deploy/nginx/ddiloco.bsv.direct.conf` → copiar a tu `user_conf.d/` del nginx-certbot
- `deploy/nginx/dash.ddiloco.bsv.direct.conf` → copiar a tu `user_conf.d/` del nginx-certbot
- `web/public/` → copiar a `/var/www/ddiloco.bsv.direct/` en el host del VPS (nginx ya monta `/var/www`)
- `deploy/docker-compose.diloco.yml` → compose de servicios DiLoCo (usa `proxy-net` externo)
- `deploy/.env.example` → copiar a `deploy/.env` en el VPS y ajustar `AGENT_TOKEN`
- `deploy/deploy_vps.sh` → script de publicación/actualización remota
- `configs/docker-coordinator.json` → config del coordinador para Docker (`host: 0.0.0.0`, puerto 8095)

## DNS

Crear registros A/AAAA en `bsv.direct` (o el proveedor DNS que uses):
- `ddiloco.bsv.direct` → IP del VPS
- `dash.ddiloco.bsv.direct` → IP del VPS

`jonasal/nginx-certbot` solicitará/renovará certificados automáticamente para los `server_name` definidos en `user_conf.d`.

## Deploy automático (recomendado)

Desde tu máquina local (con `ssh` y `rsync`):

```bash
# Ajusta rutas según dónde esté tu nginx compose en el VPS
DEPLOY_TARGET=user@vps.tls \
DEPLOY_PATH=/opt/ddiloco \
NGINX_USER_CONF_DIR=/opt/nginx/user_conf.d \
PUBLIC_WWW_DIR=/var/www/ddiloco.bsv.direct \
NGINX_CONTAINER=<nombre-o-id-del-contenedor-nginx> \
./deploy/deploy_vps.sh
```

O en formato compacto:

```bash
NGINX_USER_CONF_DIR=/opt/nginx/user_conf.d \
PUBLIC_WWW_DIR=/var/www/ddiloco.bsv.direct \
NGINX_CONTAINER=<nombre-o-id-del-contenedor-nginx> \
./deploy/deploy_vps.sh user@vps.tls:/opt/ddiloco
```

Notas:
- `NGINX_USER_CONF_DIR` debe ser la ruta **en el host del VPS** que está montada como `./user_conf.d:/etc/nginx/user_conf.d` en tu nginx compose.
- `NGINX_CONTAINER` es opcional pero recomendado para validar/recargar nginx (`docker ps` para verlo).
- El script crea `deploy/.env` en el VPS si no existe (copia de `.env.example`) y **no lo sobrescribe** después.
- Usa `DRY_RUN=1` para probar, `SKIP_BUILD=1` para no reconstruir imagen, `SKIP_NGINX=1` o `SKIP_CONTAINERS=1` según necesites.

## Deploy manual (paso a paso)

### 1) Publicar archivos estáticos y configs de nginx (en el VPS)

```bash
# Página pública (servida por nginx)
sudo mkdir -p /var/www/ddiloco.bsv.direct
sudo cp -r /opt/ddiloco/web/public/* /var/www/ddiloco.bsv.direct/

# Vhosts para nginx-certbot (ajusta /opt/nginx/user_conf.d a tu ruta real)
sudo cp /opt/ddiloco/deploy/nginx/ddiloco.bsv.direct.conf /opt/nginx/user_conf.d/
sudo cp /opt/ddiloco/deploy/nginx/dash.ddiloco.bsv.direct.conf /opt/nginx/user_conf.d/
```

### 2) Levantar contenedores DiLoCo

```bash
cd /opt/ddiloco
cp -n deploy/.env.example deploy/.env
# edita deploy/.env y pon un AGENT_TOKEN fuerte

# la red proxy-net ya existe (la usa tu nginx); por si acaso:
docker network create proxy-net || true

docker compose -f deploy/docker-compose.diloco.yml --env-file deploy/.env up -d --build
docker compose -f deploy/docker-compose.diloco.yml ps
```

### 3) Recargar nginx-certbot

```bash
docker exec <nginx-container> nginx -t
docker exec <nginx-container> nginx -s reload
# o reinicia el servicio si prefieres: docker compose restart nginx (en tu stack de nginx)
```

## Verificación

```bash
# Estado público (sin auth, sanitizado)
curl -sS https://ddiloco.bsv.direct/api/status | jq .

# Página pública
curl -I https://ddiloco.bsv.direct

# Dashboard privado (debe requerir auth si lo configuraste; siempre proteger)
curl -I https://dash.ddiloco.bsv.direct
```

Desde dentro del VPS:

```bash
docker compose -f /opt/ddiloco/deploy/docker-compose.diloco.yml logs -f --tail=100
docker exec -it diloco-coordinator python -c "print('ok')"  # sanity check
```

## Seguridad (importante)

- **No expongas** el coordinador (`8095`) ni el agente (`8765`) por puertos públicos; solo vía red Docker interna + proxy.
- **Nunca** hagas proxy público a `/v1/register`, `/v1/update`, `/v1/state`, `/launch/*`, `/stop`, etc. Solo `/api/status` → `/v1/public/status`.
- **Protege `dash.ddiloco.bsv.direct`**: habilita `auth_basic` en `deploy/nginx/dash.ddiloco.bsv.direct.conf` (y crea `.htpasswd` en `user_conf.d`) y/o restringe por IP/Tailscale. Ideal: Cloudflare Access o VPN.
- Cambia `configs/docker-coordinator.json` (`coordinator.token`) y `deploy/.env` (`AGENT_TOKEN`) por valores fuertes.
- Si el dashboard necesita consultar la API privada del coordinador desde tu navegador remoto, proxyea solo lo necesario detrás de la misma autenticación (no recomendado sin hardening adicional).

## Personalización

- **Coordinador**: edita `configs/docker-coordinator.json` (run_id, rounds, quorum, token, etc.) y recrea el contenedor.
- **HF/GPU/túneles P2P**: el `Dockerfile` base instala torch CPU. Para HF/QLoRA o bitbeam dentro del contenedor necesitarás una imagen derivada (CUDA, Node.js + `npm install` en `network/holepunch-sidecar`). Ver `docs/ONBOARDING.md`.
- **Un solo subdominio**: puedes adaptar la config para servir `/` público y `/app/` privado con auth en `ddiloco.bsv.direct` (requiere ajustar rutas/proxy).

## Problemas comunes

- **502 Bad Gateway en `/api/status`**: `diloco-coordinator` no está corriendo o no está en `proxy-net`. Revisa `docker compose ... ps/logs` y que ambos stacks compartan `proxy-net`.
- **Certificado no emitido**: DNS no propagado o `server_name` no coincide. Revisa logs del contenedor nginx-certbot.
- **`dash` accesible sin auth**: descomenta/configura `auth_basic` y asegúrate de recargar nginx.
- **Cambios en `web/public` no se ven**: vuelve a copiar a `/var/www/ddiloco.bsv.direct/` o reejecuta el script de deploy.
- **El coordinador no acepta conexiones del proxy**: verifica que `coordinator.host` sea `0.0.0.0` en la config usada dentro del contenedor.
