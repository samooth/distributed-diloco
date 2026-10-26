# Roadmap de implementación

Leyenda: `[x]` hecho, `[ ]` pendiente.

## P0 — Especificación y esqueleto

- [x] Configuración validada con fingerprint de modelo, datos y entrenamiento.
- [x] Documentación base del diseño DiLoCo WAN.
- [x] Estructura del repo y convenciones de checkpoints.

## P1 — Backend, coordinador y 2 islas

- [x] Backend `toy` determinista para CPU.
- [x] Backend `hf` con PEFT/QLoRA.
- [x] Serialización solo de parámetros entrenables.
- [x] Coordinador HTTP/1.0 con token, quórum, deadline, heartbeat.
- [x] Agregación Nesterov y checkpoints atómicos.
- [x] Reanudación desde checkpoint.
- [x] Exportación previa del adaptador inicial.
- [x] Smoke test de 2 workers en localhost.
- [x] Validación end-to-end con Qwen2.5-0.5B en CPU.

## P2.1 — Compresión int8 y sparsificación

- [x] `train.delta_compression: "int8"`.
- [x] Escala `fp32` por tensor.
- [x] Decodificación antes de agregar.
- [x] Config `configs/int8-toy.json`.
- [x] Test unitario de compresión.
- [x] Smoke int8 end-to-end.
- [x] Carga thread-safe del modelo HF.
- [x] Sparsificación top-k y error feedback implementados.
- [ ] Barrido int8/top-k de varias rondas con 2 nodos.
- [ ] Test de signo del pseudo-gradiente.

## P2.2 — Robustez de red, nodos y transporte

- [x] Rejoin con el mismo `node_id`.
- [x] Late join opcional (`coordinator.allow_late_join`).
- [x] Reintento automático si una ronda queda obsoleta.
- [ ] gRPC/HTTP2 (solo TLS opcional sobre HTTP implementado).
- [ ] TLS/mTLS.
- [x] Top-k y error feedback.
- [x] Expulsión automática de workers inactivos.
- [x] Endpoint `/v1/leave` y salida ordenada básica.
- [ ] Tolerancia a churn.
- [x] Retención de checkpoints (`checkpoint_retention`).
- [ ] Object store para checkpoints grandes.

## F0 — Pipeline y equivalencia

- [x] Ronda completa end-to-end.
- [x] Checkpoint/reload.
- [x] Equivalencia de un paso local vs baseline.
- [ ] Test de signo.
- [ ] int8 roundtrip con pérdida no explosiva a más pasos.

## F1 — Dos islas y WAN emulada

- [x] Config `configs/f1-qwen-0.5b.json`.
- [x] Config `configs/f1-qwen-0.5b-int8.json`.
- [x] Baseline centralizado con mismo presupuesto.
- [x] F1 toy: `loss_gap=-54%`.
- [x] F1 real float, H=10, 2 workers: `loss_gap=+5.26%`.
- [ ] F1 int8 de varias rondas con 2 workers.
- [ ] Barrido `H ∈ {20, 50, 100}`.
- [ ] 3 semillas para configuraciones clave.
- [ ] Pruebas con `tc netem`: latencia, pérdida y ancho de banda.
- [ ] Sync fraction <= 20%.
- [ ] int8 no empeora más de 1% la pérdida.

## F2 — Cuatro islas, 7B/8B, red real

- [ ] 4 islas con 7B/8B QLoRA.
- [ ] int8 estable.
- [ ] Inyección de nodo lento.
- [ ] Caída de nodo a mitad de ronda.
- [ ] Pérdida de red 30 s.
- [ ] Disco lleno simulado.
- [ ] Loss gap <= 5%.
- [ ] CU >= 70%, sync <= 20%.
- [ ] Recuperación <= 2 rondas.

## F3 — Piloto 32B QLoRA

- [ ] Qwen2.5-32B-Instruct en NF4.
- [ ] Islas de 24/48 GB.
- [ ] Throughput, VRAM y OOM medidos.
- [ ] Loss gap vs 32B single-node <= 3%.
- [ ] Compile rate Zig vs baseline.

## F4 — Piloto 70B QLoRA

- [ ] Llama-3.1-70B-Instruct en NF4.
- [ ] Islas de 48/80 GB.
- [ ] Coste por 1B tokens.
- [ ] Loss gap vs 70B single-node <= 3%.
- [ ] Comparación de calidad frente a 32B.

## F5 — Full fine-tuning 7B/13B

- [ ] FSDP2 dentro de la isla.
- [ ] DiLoCo entre islas.
- [ ] Checkpoint shardeado y reanudación.
- [ ] Loss gap <= 5%.

## F6 — Escala e investigación

- [ ] 8-20 nodos.
- [ ] 32B/70B full o 100B+.
- [ ] Solo con caso y presupuesto definidos.

## Ablaciones pendientes

- [ ] `H`: 20, 50, 100, 200, 500.
- [ ] `M`: 2, 4, 8.
- [ ] `η`: 0.2, 0.4, 0.7, 1.0.
- [ ] `γ`: 5e-5, 1e-4, 2e-4.
- [ ] Compresión: bf16, int8, 4-bit.
- [ ] Adaptador: q/v, atención, todas las lineales.
- [ ] Rango: 8, 16, 32.
- [ ] Datos: IID, estratificado, no-IID.
- [ ] Topología: estrella, anillo.
