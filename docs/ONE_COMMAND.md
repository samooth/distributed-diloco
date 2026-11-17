# Flujo one-command (sin editar archivos a mano)

> Estado: implementado y desplegado; suite completa en verde (92 tests) y E2E
> `pilot up` → `pilot join` verificado con 2 rondas completadas.

Con `scripts/pilot.py` un run completo son **dos comandos**. No hace falta crear
configs, copiar claves ni editar allowlists.

## 1. Quien organiza (coordinador)

```bash
.venv/bin/python scripts/pilot.py up --name demo --preset toy --workers 2
```

`up` hace todo esto solo:

1. Comprueba que el proyecto está listo.
2. Crea `runs/demo/identity.json` (clave BSV; **privada**, modo `0600`).
3. Genera `runs/demo/config.json` desde el preset elegido (`toy`, `7b-lora`,
   `32b-qlora`, `70b-qlora`).
4. Arranca el coordinador con túnel bitbeam de identidad y un **token de unión**
   aleatorio, con caducidad (1 h) y un máximo de usos igual a `--workers`.
5. Imprime **una sola línea** para compartir y su QR (`runs/demo/join.svg`).

Ejemplo de línea:

```
didiloco://join?h=http%3A%2F%2F192.168.1.10%3A8095&t=<token>&r=demo&k=<pubkey>
```

## 2. Quien se une (cada worker)

```bash
.venv/bin/python scripts/pilot.py join 'didiloco://join?h=...' --node-id worker-a
```

`join` hace todo esto solo:

1. Verifica la firma de la invitación (descargada del coordinador con el token).
2. Crea `runs/worker-a/identity.json` si no existe (modo `0600`).
3. Firma una solicitud de admisión y llama a `/v1/join`.
4. Escribe localmente la **config exacta** (firmada por el coordinador), así que
   el fingerprint siempre coincide.
5. Arranca el worker con todos los flags derivados de la invitación.

Para LAN o pruebas sin DHT añade `--no-tunnel` y se conecta directo al endpoint.

## 3. Ver el estado

```bash
.venv/bin/python scripts/pilot.py status --url http://127.0.0.1:8095
```

Muestra ronda, workers, quórum, timeout efectivo y el último resultado, con
mensajes en lenguaje claro ante errores habituales.

## Dashboard

El panel **Launcher** tiene una tarjeta *"Unirse en 1 clic"*: pegas la línea y
pulsas *Unirse*. El agente guarda el código en un fichero `0600` y lanza
`pilot.py join` con la ruta, para que el token **no aparezca en logs ni en el
comando del proceso**.

## Modelo de confianza (importante)

- La identidad privada (WIF) **nunca sale de su máquina**. Solo se comparte la
  clave pública.
- La invitación es un **token al portador**: quien la tiene puede unirse. Por eso
  caduca y tiene un número máximo de usos. Trátala como una contraseña: no la
  publiques en canales abiertos.
- El código/QR contiene el token. Guárdalo con el mismo cuidado.
- La primera vez que un worker ve la clave pública del coordinador es
  *trust-on-first-use* (TOFU): verifica la firma de la invitación, pero la
  confianza en esa clave debe confirmarse por el canal por el que recibiste
  la invitación. Para mayor rigor, compara el prefijo `k=...` del código con
  la clave pública que te dio el coordinador por otro medio.

## Troubleshooting

| Mensaje | Causa / solución |
|---|---|
| "Token de unión no válido, caducado o ya usado" | Pide una invitación nueva. |
| "Tu clave pública no está autorizada" | El worker se unió con otra identidad; repite `join`. |
| "La configuración no coincide" | Usa la config que viene en la invitación (la escribe `join`). |
| "No se puede contactar con el coordinador" | Revisa el endpoint del código y el firewall. |
| "La sesión ya empezó o terminó" | Pide al coordinador reiniciar el run e invitar de nuevo. |
| El túnel no conecta | Añade `--no-tunnel` si estáis en la misma LAN. |
