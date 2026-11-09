# ◆ DiLoCo WAN

> **在互联网上训练大模型 LoRA 适配器，无需数据中心。**

DiLoCo WAN 使用 **DiLoCo**（*Distributed Low-Communication*）：每 `H` 个本地步仅同步 LoRA 适配器，外部使用 Nesterov 优化器。基础模型冻结（NF4/QLoRA）。

## 特性
- **后端**：`toy`（确定性 CPU）+ `hf`（PEFT/QLoRA）。
- **协调器**：HTTP/gRPC、token、quorum、deadline、heartbeat、late join、`/v1/state_meta`。
- **压缩**：int8（4×）、top-k + error feedback。
- **Churn**：rejoin、驱逐、≤2 轮恢复（已测试）。
- **P2P/WAN**：**bitbeam v4**（DHT）持久化多路复用隧道（`--p2p-topic`），WAN 仿真。
- **HF 启动**：自动发现 `runs/init-<config-stem>`，`--init-state auto`，哈希匹配时本地 bootstrap。
- **UX**：根目录封装 `./coordinator` `./worker` `./agent`，向导，Web 控制台/监控。

## 快速开始
```bash
.venv/bin/python -m unittest discover -s tests -q
./coordinator --config configs/smoke.json --device cpu --reset --max-rounds 4 --no-wait
./worker --config configs/smoke.json --node-id node-0 --coordinator http://127.0.0.1:8095 --device cpu
./agent --open  # 提供 web/ 并打开 dashboard
```
P2P 隧道：`cd network/holepunch-sidecar && npm install`，然后为协调器/worker 添加 `--p2p-topic <topic>`。仓库配置使用占位路径。

## 文档
- [Onboarding](../ONBOARDING.md) · [通信](../03-comunicacion.md) · [安全](../07-seguridad.md)
- [Changelog](../../CHANGELOG.md) · [状态](../../STATUS.md) · [路线图](../../ROADMAP.md)
