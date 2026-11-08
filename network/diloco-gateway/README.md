# Multiplayer Gateway (space.bsv.direct) para DiLoCo

Gateway Node.js reutilizado de `space.bsv.direct`: WebSocket + Hyperswarm,
rooms, relay de mensajes `diloco-*`, firma BSM opcional y rate limiting.

## Cómo encaja con DiLoCo

- El coordinador Python sigue siendo la autoridad de agregación/checkpoints.
- El gateway actúa como **relay WebSocket/Hyperswarm**: los nodos pueden
  intercambiar deltas, checkpoints y estado de ronda sin exponer IPs.
- Los mensajes con prefijo `diloco-` se retransmiten a todos los peers del
  room y al swarm Hyperswarm asociado.
- La firma BSM (`msg.sig` + `identityKey`) permite autenticar deltas en el
  gateway antes de retransmitirlos.

## Arranque

```bash
cd network/multiplayer-gateway
npm install
PORT=8080 node gateway.js
```

## Uso con DiLoCo

1. Levanta el coordinador Python (HTTP/gRPC) en una máquina.
2. Levanta el gateway en una máquina accesible (o en la misma).
3. Los workers se conectan al gateway por WebSocket y envían mensajes
   `diloco-update`, `diloco-state`, etc.
4. El gateway hace broadcast a los peers y al swarm Hyperswarm.

## Variables de entorno útiles

- `PORT`: puerto HTTP/WebSocket (default 8080).
- `REQUIRES_AUTH=1`: exige firma BSM en mensajes `input`.
- `DEBUG=1`: logs detallados.
- `ALLOWED_ORIGINS`: orígenes CORS permitidos.

## Limitaciones actuales

- El gateway no reemplaza al coordinador Python; la agregación y el outer
  optimizer siguen en `diloco/`.
- Para usar el gateway como transporte principal haría falta un adaptador
  WebSocket en `diloco/worker.py` y `diloco/coordinator.py`.
- Hoy se usa como capa de discovery/relay y distribución de artefactos.
EOF
echo done