# ◆ DiLoCo WAN

> **データセンターなしで、インターネット越しに大規模モデルの LoRA アダプタを学習。**

DiLoCo WAN は **DiLoCo** (*Distributed Low-Communication*) を用い、`H` ローカルステップごとに LoRA アダプタのみを同期し、外部 Nesterov オプティマイザで集約します。ベースモデルは凍結（NF4/QLoRA）です。

## 特徴
- **バックエンド**: `toy`（決定的 CPU）+ `hf`（PEFT/QLoRA）。
- **コーディネータ**: HTTP/gRPC、token、quorum、deadline、heartbeat、late join、`/v1/state_meta`。
- **圧縮**: int8（4×）、top-k + error feedback。
- **Churn**: rejoin、eviction、≤2 ラウンドで回復（テスト済み）。
- **P2P/WAN**: **bitbeam v4**（DHT）永続マルチプレックストンネル（`--p2p-topic`）、WAN エミュレーション。
- **HF ブートストラップ**: `runs/init-<config-stem>` 自動検出、`--init-state auto`、ハッシュ一致時のローカル bootstrap。
- **UX**: ルートラッパー `./coordinator` `./worker` `./agent`、wizard、Web コンソール/モニタ。

## クイックスタート
```bash
.venv/bin/python -m unittest discover -s tests -q
./coordinator --config configs/smoke.json --device cpu --reset --max-rounds 4 --no-wait
./worker --config configs/smoke.json --node-id node-0 --coordinator http://127.0.0.1:8095 --device cpu
./agent --open  # web/ を配信して dashboard を開く
```
P2P トンネル: `cd network/holepunch-sidecar && npm install` の後、coordinator/worker に `--p2p-topic <topic>` を指定。リポジトリの config はパスのプレースホルダを使用します。

## ドキュメント
- [Onboarding](../ONBOARDING.md) · [通信](../03-comunicacion.md) · [セキュリティ](../07-seguridad.md)
- [Changelog](../../CHANGELOG.md) · [状態](../../STATUS.md) · [Roadmap](../../ROADMAP.md)
