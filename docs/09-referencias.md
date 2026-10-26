# 09. Referencias comentadas

Las cifras de los papers no son directamente comparables: distintas escalas,
hardware, datasets, número de réplicas y presupuestos de tokens. Úsalas para
entender el régimen, no como benchmark de este proyecto.

## 1. Algoritmo y sistemas distribuidos

### DiLoCo

- **Paper:** *DiLoCo: Distributed Low-Communication Training of Language
  Models* (Douillard et al., Google DeepMind, 2023; v2 2024).
- **Enlace:** <https://arxiv.org/abs/2311.08105>
- **Aporte:** AdamW interior + Nesterov exterior; ~500× menos comunicación que
  DP en 8 workers y hasta 400M de parámetros; robusto a datos no-IID y a
  recursos que aparecen/desaparecen.
- **Limitación:** modelos pequeños; `H=500`; no es un sistema de producción.

### OpenDiLoCo

- **Paper:** *OpenDiLoCo: An Open-Source Framework for Globally Distributed
  Low-Communication Training* (Jaghouar et al., Prime Intellect, 2024).
- **Enlace:** <https://arxiv.org/abs/2407.07852>
- **Repo:** <https://github.com/PrimeIntellect-ai/OpenDiLoCo>
- **Aporte:** implementación reproducible, 1.1B, 4 workers, 90-95% CU en 3
  continentes, fp16 sin degradación, `H=125` casi iguala DP con 125× menos
  comunicación.
- **Limitación:** 4 workers; `H=500` empeora; el repo ya no se mantiene.

### prime / prime-diloco

- **Repo:** <https://github.com/PrimeIntellect-ai/prime-diloco>
- **Aporte:** `ElasticDeviceMesh`, FSDP2, int8 all-reduce, CPU offload, live
  checkpoint recovery, heartbeat/deathrattle, checkpoint asíncrono.
- **Uso:** referencia para full fine-tuning y tolerancia a fallos. Para LoRA,
  un trainer propio es más simple.
- **Nota:** verificar licencia, estado y compatibilidad antes de adoptarlo.

### INTELLECT-1

- **Paper:** *INTELLECT-1 Technical Report* (Prime Intellect, 2024).
- **Enlace:** <https://arxiv.org/abs/2412.01152>
- **Aporte:** 10B, 1T tokens, hasta 14 nodos, 3 continentes; 83-96% CU,
  36.2-41.4% MFU; int8, `H=100`; 400× menos comunicación.
- **Limitación:** preentrenamiento con H100; no es fine-tuning doméstico.

### INTELLECT-2

- **Paper:** *INTELLECT-2: A Reasoning Model Trained Through Globally
  Decentralized Reinforcement Learning* (Prime Intellect, 2025).
- **Enlace:** <https://arxiv.org/abs/2505.07291>
- **Aporte:** RL distribuido de 32B; infraestructura asíncrona, `TOPLOC`,
  `shardcast`; muestra que 32B global es posible en RL.
- **Limitación:** RL asíncrono, no DiLoCo de preentrenamiento; no se traslada
  1:1 a SFT.

### INTELLECT-3

- **Paper:** *INTELLECT-3 Technical Report* (Prime Intellect, 2025).
- **Enlace:** <https://arxiv.org/abs/2512.16144>
- **Aporte:** 106B MoE, 12B activos, RL a gran escala con `prime-rl`.
- **Limitación:** RL, no entrenamiento DiLoCo; útil como estado del arte de
  infraestructura.

### Streaming DiLoCo

- **Paper:** *Streaming DiLoCo with overlapping communication: Towards a
  Distributed Free Lunch* (Douillard et al., Google, 2025).
- **Enlace:** <https://arxiv.org/abs/2501.18512>
- **Aporte:** sincroniza fragmentos, solapa comunicación con cómputo y cuantiza
  a 4 bits; similar calidad a DP con ~400× menos ancho de banda.
- **Limitación:** complejidad; no es la primera opción para adaptadores.

### SparseLoCo

- **Paper:** *Communication Efficient LLM Pre-training with SparseLoCo*
  (Sarfi et al., 2025; NeurIPS 2025).
- **Enlace:** <https://arxiv.org/abs/2508.15706>
- **Aporte:** top-k por bloques + 2 bits + error feedback; 97-99% sparsity;
  supera a DiLoCo denso en 178M-2B y 8-32 workers.
- **Limitación:** preentrenamiento; no probado a 70B; el error feedback
  reemplaza el momento exterior y requiere tuning.

### DeMo

- **Paper:** *DeMo: Decoupled Momentum Optimization* (Peng et al., 2024;
  ICLR 2026).
- **Enlace:** <https://arxiv.org/abs/2411.19870>
- **Repo:** <https://github.com/bloc97/DeMo>
- **Aporte:** DCT + top-k sobre el momento, con el buffer como error feedback;
  85× menos datos por GPU en 300M; 44× en 1B.
- **Limitación:** comprime cada paso; no reduce la frecuencia de sincronización
  como DiLoCo. No es el mismo régimen.

### MuLoCo

- **Paper:** *MuLoCo: Muon is a practical inner optimizer for DiLoCo* (2025).
- **Enlace:** <https://arxiv.org/abs/2505.23725>
- **Aporte:** Muon interior + 2 bits + error feedback; 8× menos comunicación
  que DiLoCo con la misma memoria.
- **Limitación:** Muon no es estándar; requiere validación.

### DiLoCoX

- **Paper:** *DiLoCoX: A Low-Communication Large-Scale Training Framework for
  Decentralized Cluster* (Qi et al., 2025).
- **Enlace:** <https://arxiv.org/abs/2506.21263>
- **Aporte:** pipeline parallelism + dual optimizer + one-step-delay overlap +
  compresión adaptativa; 107B sobre 1 Gbps simulado, 357× throughput vs
  AllReduce.
- **Limitación:** prueba de concepto (WikiText-103, 4.000 pasos, 1 Gbps
  simulado con `tc`); no es una receta doméstica.

### Scaling Laws for DiLoCo

- **Paper:** *Communication-Efficient Language Model Training Scales Reliably
  and Robustly: Scaling Laws for DiLoCo* (Charles et al., 2025; NeurIPS 2025).
- **Enlace:** <https://arxiv.org/abs/2503.09799>
- **Aporte:** 35M-2.4B, validación 4B/10B; `M=2` supera a DP con >100× menos
  comunicación; el batch óptimo crece con `M`; DiLoCo mejora incluso sin cuello
  de comunicación (`M=1`, tipo Lookahead).
- **Limitación:** `H=30`; preentrenamiento; no LoRA.

### Photon

- **Paper:** *Photon: Federated LLM Pre-Training* (Sani et al., MLSys 2025).
- **Enlace:** <https://proceedings.mlsys.org/paper_files/paper/2025/hash/185087ea328b4f03ea8fd0c8aa96f747-Abstract-Conference.html>
- **Aporte:** preentrenamiento federado hasta 7B, 64-512× menos comunicación,
  35% mejor wall-time que baselines; 2× más rápido que DiLoCo en convergencia.
- **Limitación:** investigación; no es un framework maduro para 70B.

### Factored Gossip DiLoCo

- **Paper:** *Factored Gossip DiLoCo: Reducing Blocking Communication in
  DiLoCo* (Hewa Koneputugodage et al., 2026).
- **Enlace:** <https://proceedings.mlr.press/v306/hewa-koneputugodage26a.html>
- **Aporte:** sincronización aproximada con mezcla/gossip; más robusto a
  stragglers y fallos; mejor CU en modelos de hasta 1B.
- **Limitación:** nuevo; la convergencia y la reproducibilidad son más
  difíciles.

## 2. Fine-tuning eficiente

### LoRA

- **Paper:** *LoRA: Low-Rank Adaptation of Large Language Models* (Hu et al.,
  2021).
- **Enlace:** <https://arxiv.org/abs/2106.09685>
- **Aporte:** adaptadores de bajo rango; base congelada.

### QLoRA

- **Paper:** *QLoRA: Efficient Finetuning of Quantized LLMs* (Dettmers et al.,
  2023).
- **Enlace:** <https://arxiv.org/abs/2305.14314>
- **Aporte:** NF4 + doble cuantización + LoRA; 65B en 48 GB; base congelada.
- **Uso:** ruta principal para 32B/70B en nodos domésticos.

### DoRA

- **Paper:** *DoRA: Weight-Decomposed Low-Rank Adaptation* (Liu et al., 2024).
- **Enlace:** <https://arxiv.org/abs/2402.09353>
- **Aporte:** descompone magnitud y dirección; puede mejorar con rangos bajos.
- **Limitación:** más parámetros/cómputo; no compatible con algunas rutas MoE de
  PEFT.

### FSDP / ZeRO

- **FSDP docs:** <https://pytorch.org/docs/stable/fsdp.html>
- **ZeRO paper:** <https://arxiv.org/abs/1910.02054>
- **Aporte:** sharding de pesos, gradientes y estados; necesario para full
  fine-tuning.
- **Uso:** dentro de una isla, nunca a través de WAN en v1.

## 3. Herramientas

| Herramienta | Enlace | Uso |
|---|---|---|
| PyTorch | <https://pytorch.org/> | Framework base |
| Transformers | <https://github.com/huggingface/transformers> | Modelos y tokenizers |
| PEFT | <https://github.com/huggingface/peft> | LoRA/DoRA |
| bitsandbytes | <https://github.com/bitsandbytes-foundation/bitsandbytes> | NF4, optimizadores |
| Accelerate | <https://github.com/huggingface/accelerate> | Carga y device maps |
| safetensors | <https://github.com/huggingface/safetensors> | Serialización segura |
| datasets | <https://github.com/huggingface/datasets> | Datasets |
| vLLM | <https://github.com/vllm-project/vllm> | Serving con LoRA |
| Hivemind | <https://github.com/learning-at-home/hivemind> | Referencia P2P/all-reduce |
| Tailscale | <https://tailscale.com/> | VPN mesh/NAT |
| WireGuard | <https://www.wireguard.com/> | VPN |
| iperf3 | <https://software.es.net/iperf/> | Medir ancho de banda |
| Prometheus | <https://prometheus.io/> | Métricas |
| Grafana | <https://grafana.com/> | Dashboards |
| MinIO | <https://min.io/> | Object store S3 |
| gRPC | <https://grpc.io/> | Transporte |

## 4. Datos y evaluación del repo

- `moe_zig/eval.py`: evaluación con compile real y CE completion-only.
- `data/zig_dense_v5_module_clean.jsonl`: train limpio.
- `data/zig_holdout.jsonl`: holdout de módulos.
- `EXPERIMENTS.md`: resultados y limitaciones del lab.
- `README.md`: estado de los modelos y datasets.

Para el piloto distribuido, reutilizar estas piezas es útil, pero el holdout es
pequeño y se ha usado para varias decisiones. No presentar una mejora de
compile como conclusión general sin un test adicional congelado.

## 5. Cómo leer las cifras

- **“500× menos comunicación”** suele comparar con data parallelism por paso,
  no con otro DiLoCo. Incluye `H` y, a veces, cuantización.
- **“90-95% CU”** depende del hardware, el tamaño de ronda y la red; no se
  traslada a fibra doméstica.
- **“1 Gbps”** en DiLoCoX es simulado con `tc`; no es internet real.
- **“NF4 70B en 48 GB”** es ajustado; el paper de QLoRA usa 65B, no 70B, y
  hardware de datacenter.
- **“Loss gap”** depende del número de tokens, el baseline y el dataset. Un
  gap del 1% en un modelo pequeño no garantiza lo mismo a 70B.
- **“int8 sin pérdida”** se observó en preentrenamiento de 10B; en fine-tuning
  de adaptadores hay que medirlo.
- **“SparseLoCo supera a DiLoCo”** es en preentrenamiento de 178M-2B; no es una
  garantía para LoRA de 70B.
- **“OpenDiLoCo ya no se mantiene”**; usar `prime` o el paper como referencia,
  no como dependencia de producción sin evaluar.

## 6. Lecturas mínimas

Si solo hay tiempo para cinco:

1. DiLoCo (2311.08105).
2. OpenDiLoCo (2407.07852).
3. INTELLECT-1 (2412.01152).
4. QLoRA (2305.14314).
5. Scaling Laws for DiLoCo (2503.09799).

Después: Streaming DiLoCo, SparseLoCo y DiLoCoX, en ese orden, si se pasa a
full fine-tuning o a 100B+.
