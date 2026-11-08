# ◆ DiLoCo WAN

> **在互联网上训练 32B/70B LoRA 适配器，无需数据中心。**

DiLoCo WAN 使用 **DiLoCo**（*Distributed Low-Communication*）适配大模型：
每 `H` 个本地步只同步适配器，外部使用 Nesterov 优化器。基础模型
冻结在 NF4（QLoRA），每座“岛”可运行在消费级 GPU（8–80 GB）上，
每轮流量从 **140 GB 降至 0.07–0.8 GB**。

## 特性

- **后端**：`toy`（确定性 CPU）+ `hf`（PEFT/QLoRA），线程安全加载。
- **协调器**：HTTP/gRPC、token、quorum、deadline、heartbeat、late join。
- **压缩**：int8（4×）、top-k + error feedback。
- **Churn**：rejoin、驱逐、≤2 轮恢复（含集成测试）。
- **安全**：TLS/mTLS、allowlist、delta 校验。
- **存储**：原子 checkpoint、保留策略、本地/S3 object store。
- **WAN**：仿真（延迟、抖动、丢包、带宽）、Hyperswarm 隧道。
- **Web**：展示页 + 运维控制台 + 网络监控（响应式）。
- **CLI**：经典向导与 TUI（curses）。
- **Agent**：从 Web UI 启动进程。

## 快速开始

```bash
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python scripts/smoke_local.py --reset --rounds 4 --workers 2 --device cpu
.venv/bin/python scripts/wizard.py
```

## 文档

- [Onboarding](../ONBOARDING.md) — 从零到第一轮。
- [算法](../01-algoritmo.md) · [架构](../02-arquitectura.md) · [通信](../03-comunicacion.md)
- [模型](../04-modelos.md) · [训练](../05-entrenamiento.md) · [运维](../06-operaciones.md)
- [安全](../07-seguridad.md) · [实验](../08-experimentos.md) · [参考](../09-referencias.md)
- [Changelog](../../CHANGELOG.md) · [状态](../../STATUS.md) · [路线图](../../ROADMAP.md)