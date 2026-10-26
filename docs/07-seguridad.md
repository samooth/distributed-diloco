# 07. Seguridad, privacidad y confianza

Un sistema distribuido por internet no es seguro por defecto. Este documento
define qué se protege, de quién y con qué límites. La postura de v1 es
**permissionada**: solo nodos conocidos, con allowlist y mTLS. La postura
permissionless requiere verificación de cómputo y robustez que todavía no están
resueltas.

## 1. Activos

| Activo | Por qué importa |
|---|---|
| Pesos base y adaptadores | Propiedad intelectual; coste de entrenamiento |
| Datos de entrenamiento | Privacidad, licencias, secretos |
| Credenciales y claves | Acceso al coordinador, object store, TLS y nodos |
| Integridad del modelo | Un adaptador envenenado puede ser peligroso |
| Disponibilidad | Un nodo puede bloquear o inundar el run |
| Reputación | Un run público con datos filtrados es un incidente |

## 2. Actores y confianza

| Actor | Confianza | Capacidades |
|---|---|---|
| Operador del coordinador | Alta | Ve metadatos, puede parar el run |
| Operador de isla | Media/alta | Ve su shard y el adaptador global |
| Voluntario permissionless | Baja | Puede enviar deltas maliciosos, mentir, abandonar |
| Atacante de red | Nula | Interceptar, replay, DoS |
| Proveedor cloud/VPN | Baja/media | Ver tráfico si no hay TLS; logs |

v1 solo admite actores de confianza alta/media. v2 podrá admitir voluntarios
con controles, pero no se promete seguridad permissionless.

## 3. Modelo de amenazas

### 3.1 Envenenamiento del modelo

Un nodo entrena con datos modificados o envía un delta manipulado para
introducir un backdoor, sesgo o degradación.

Señales:

- Norma del delta muy superior a la mediana.
- Cosine similarity negativa con la media.
- Pérdida local anómala (muy baja o muy alta).
- Cambio brusco en la evaluación canario.
- Delta con valores fuera de rango o tensores con nombres inesperados.

Controles v1:

- Allowlist de nodos.
- Clip de norma del delta.
- Validación de shape/dtype/rango.
- Evaluación canario antes de confirmar la ronda.
- Revisión manual de deltas anómalos.
- Rollback si la evaluación empeora.

Controles v2:

- Agregación robusta (mediana por coordenada, trimmed mean).
- Reputación y staking.
- Auditoría aleatoria de cómputo.
- Redundancia de shards y comparación.
- Verificación de aprendizaje (problema abierto).

Ningún control es suficiente contra un adversario con muchos nodos
(Byzantine majority). No presentar esto como “seguro” sin cuantificar el
supuesto.

### 3.2 Exfiltración de datos

Un nodo ve su shard en claro. Si el dato es sensible, el operador del nodo
puede copiarlo.

Controles:

- Solo datos públicos o con licencia para este uso.
- Nodos de confianza con acuerdos legales.
- Minimización: cada nodo ve solo lo que necesita.
- Cifrado en reposo y en tránsito; no protege de un nodo que ya descifra.
- DP-SGD o agregación segura si se necesita privacidad formal; complejo con
  DiLoCo y no es v1.
- No enviar datos al coordinador; solo hashes/índices.

### 3.3 Robo del modelo

El adaptador global se distribuye a todos los nodos. Un nodo puede copiarlo.

Controles:

- Asumir que el adaptador es visible para los nodos; no es un secreto.
- Si el adaptador es propiedad intelectual, usar nodos de confianza o
  computación confidencial.
- Firmar y marcar artefactos; no impide copiar.
- Licencia de contribución clara.

### 3.4 Replay y confusion

Un atacante reenvía un `Update` viejo o de otro run.

Controles:

- `run_id`, `round`, `nonce` y `base_hash` dentro de la firma.
- El coordinador rechaza rondas cerradas, hashes desconocidos y firmas
  inválidas.
- Idempotencia por `(run_id, round, node_id)`.
- Ventana de validez temporal.

### 3.5 Suplantación y Sybil

Un atacante crea muchos nodos falsos para dominar la agregación.

Controles:

- Allowlist y identidad verificada en v1.
- Coste de entrada (staking, reputación, invitación) en v2.
- Límite de influencia por operador, no solo por nodo.
- Diversidad de datos y de infraestructura.

### 3.6 DoS

Un nodo inunda el coordinador con subidas, conexiones o mensajes grandes.

Controles:

- Rate limit por nodo y por IP.
- Tamaño máximo de payload y de tensor.
- Timeouts y deadlines.
- Cola acotada; backpressure.
- TLS terminado en un proxy con límites.
- Monitorizar y bloquear patrones anómalos.

### 3.7 Cadena de suministro

Imágenes, modelos o dependencias comprometidas.

Controles:

- Imágenes por digest, no `latest`.
- Dependencias fijadas con hashes; `pip --require-hashes`.
- No `trust_remote_code=True`; auditar el código del modelo.
- Verificar hashes de la base y de los datos.
- SBOM y escaneo de vulnerabilidades.
- No ejecutar scripts de descarga arbitrarios.

### 3.8 Coordinador comprometido

El coordinador puede ver todos los deltas y alterar el modelo global.

Controles:

- Acceso restringido y auditado.
- Firmar los manifiestos de ronda; las islas verifican.
- Checkpoints replicados; comparar hashes entre islas.
- Separación de roles y principio de mínimo privilegio.
- En v2, outer replicado y verificación mutua.

## 4. Controles por capa

### 4.1 Identidad y acceso

- mTLS con certificados de nodo emitidos por una CA interna.
- Certificados de corta duración (7-30 días) y rotación.
- Tokens de run con alcance limitado.
- RBAC en el coordinador: `admin`, `operator`, `viewer`, `worker`.
- Tailscale ACLs o WireGuard `AllowedIPs`.
- 2FA para el panel de administración.

### 4.2 Integridad

- SHA256 de base, datos, adaptador y payload.
- Firma Ed25519 de cada `Update`.
- Manifiesto de ronda firmado por el coordinador.
- Validación de shapes, dtypes, `numel` y rangos.
- Comparación periódica del hash del adaptador entre islas.
- Checkpoints inmutables y versionados.

### 4.3 Confidencialidad

- TLS 1.3 en tránsito.
- Cifrado en reposo del object store y de los discos.
- Datos públicos o de confianza; DP si hace falta.
- No loggear tensores, prompts ni datos personales.
- Rotación de secretos; gestor de secretos.

### 4.4 Disponibilidad

- Rate limits, cuotas y deadlines.
- Coordinador con reinicio automático y backup.
- Object store con replicación.
- Alertas de caída y de saturación.
- Plan de rollback.

## 5. Privacidad

### 5.1 Regla de oro

Si los datos no pueden ser vistos por el operador de un nodo, no se entrenan
en ese nodo. DiLoCo no cambia eso: el nodo necesita los datos en claro para
calcular gradientes.

### 5.2 Opciones

| Opción | Privacidad | Coste | Madurez |
|---|---|---|---|
| Datos públicos | N/A | Bajo | Alta |
| Nodos de confianza + contrato | Media | Medio | Alta |
| DP-SGD + DiLoCo | Formal, con epsilon | Alto; degrada calidad | Baja/media |
| Agregación segura | Oculta deltas al coordinador | Alto; no oculta datos locales | Media |
| Computación confidencial (TEE) | Alta | Muy alto; hardware específico | Baja |

No afirmar “privacidad” sin definir el adversario y el épsilon. Para v1, usar
datos públicos o nodos de confianza.

### 5.3 Datos personales

- Minimizar; no incluir PII innecesaria.
- Anonimizar/pseudonimizar antes de shardear.
- Revisar licencias y bases legales (GDPR si aplica).
- Registrar el origen y la licencia de cada shard.
- Borrado: si un dato se elimina, hay que poder retirarlo de futuros shards;
  los checkpoints ya entrenados no “olvidan” automáticamente.

## 6. Permissionless: lo que no está resuelto

Abrir el sistema a voluntarios anónimos requiere:

1. **Verificar que el nodo entrenó de verdad.** No basta con firmar un delta.
2. **Verificar que usó los datos asignados.** Difícil sin revelar datos.
3. **Resistir Sybil y colusión.** La mayoría Byzantine no se resuelve con
   clipping.
4. **Proteger la privacidad de los datos.** El nodo ve el shard.
5. **Evitar free-riders.** Un nodo puede enviar un delta trivial.
6. **Medir reputación sin incentivos perversos.**

Prime Intellect usa `TOPLOC` para verificar inferencia en RL, no para
entrenamiento. La verificación de cómputo de entrenamiento sigue siendo un
problema abierto. Por eso v1 es permissionado.

## 7. Legal y licencias

- **Modelo base:** respetar la licencia (Llama, Qwen, Apache, etc.) y los
  términos de uso.
- **Datos:** licencia de cada corpus; no asumir que “está en internet” implica
  permiso de entrenamiento.
- **Contribuciones:** acuerdo de contribución que aclare titularidad, uso y
  publicación del adaptador.
- **Exportación:** cumplir controles de exportación si aplica.
- **GDPR/privacidad:** base legal, minimización, retención, derechos de los
  interesados.
- **Seguridad:** no prometer resultados legales; consultar a un profesional.

## 8. Respuesta a incidentes

1. **Detectar:** alerta de norma anómala, pérdida, hash o heartbeat.
2. **Contener:** expulsar el nodo, revocar certificado, pausar el run.
3. **Preservar:** logs, deltas, checkpoints, imágenes.
4. **Evaluar:** ¿afecta a la integridad del modelo? ¿hay datos personales?
5. **Erradicar:** rotar claves, parchear, bloquear.
6. **Recuperar:** rollback a checkpoint anterior; reanudar con nodos
   auditados.
7. **Aprender:** postmortem sin culpables; actualizar controles.

## 9. Checklist de seguridad

### Antes de abrir un nodo

- [ ] Identidad verificada y allowlist.
- [ ] Certificado emitido y ACLs.
- [ ] Imagen por digest y auditable.
- [ ] Base y datos con hash verificado.
- [ ] Contenedor sin privilegios, sin Docker socket.
- [ ] Egress limitado.
- [ ] Disco cifrado si hay datos sensibles.
- [ ] Acuerdo de contribución y licencias revisadas.

### Antes de un run público

- [ ] Datos públicos o de confianza.
- [ ] Evaluación canario y rollback.
- [ ] Rate limits y cuotas.
- [ ] Monitorización y alertas.
- [ ] Plan de incidentes.
- [ ] Revisión legal.
- [ ] Presupuesto para nodos maliciosos.

### Después de un incidente

- [ ] Nodo aislado y clave revocada.
- [ ] Run pausado y checkpoint preservado.
- [ ] Rollback evaluado.
- [ ] Logs y deltas archivados.
- [ ] Notificación si corresponde.
- [ ] Postmortem y acciones.

## 10. Decisiones de seguridad

| ID | Decisión | Motivo |
|---|---|---|
| S-01 | v1 permissionado | Verificación permissionless no resuelta |
| S-02 | mTLS + allowlist | Identidad y acceso simples |
| S-03 | Firmar updates y manifiestos | Integridad y no repudio |
| S-04 | Clip + canario + rollback | Defensa en profundidad contra poisoning |
| S-05 | Datos públicos o de confianza | DiLoCo no da privacidad |
| S-06 | Sin `pickle` ni `trust_remote_code` | Superficie de ataque |
| S-07 | v2 con agregación robusta | Necesario para abrir nodos |
| S-08 | No prometer DP ni TEE | No implementado; sería marketing |

## 11. Riesgos residuales

- Un operador de confianza puede copiar el adaptador.
- Un nodo comprometido puede degradar el modelo antes de ser detectado.
- La evaluación canario no detecta todos los backdoors.
- El coordinador sigue siendo un punto único de confianza en v1.
- La privacidad formal no está garantizada.
- La licencia de los datos puede ser incompatible con el uso comercial.
