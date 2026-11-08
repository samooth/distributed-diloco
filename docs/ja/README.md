# ◆ DiLoCo WAN

> **データセンターなしで、インターネット上で 32B/70B LoRA アダプタを学習。**

DiLoCo WAN は **DiLoCo** (*Distributed Low-Communication*) で大規模モデルを適応します。
`H` ローカルステップごとにアダプタのみを同期し、外部は Nesterov オプティマイザ。
ベースは NF4 (QLoRA) で凍結されるため、各島は民生 GPU (8–80 GB) に収まり、
1 ラウンドのトラフィックは **140 GB から 0.07–0.8 GB** に減少します。

## 特徴

- **バックエンド**: `toy`（決定的 CPU）+ `hf`（PEFT/QLoRA）、スレッドセーフ読み込み。
- **コーディネータ**: HTTP/gRPC、token、quorum、deadline、heartbeat、late join。
- **圧縮**: int8（4×）、top-k + error feedback。
- **Churn**: rejoin、eviction、≤2 ラウンドで回復（統合テスト済み）。
- **セキュリティ**: TLS/mTLS、allowlist、delta 検証。
- **ストレージ**: アトミック checkpoint、保持、ローカル/S3 object store。
- **WAN**: エミュレーション（遅延、ジッタ、損失、帯域）、Hyperswarm トンネル。
- **Web**: ランディング + 運用コンソール + ネットワークモニタ（レスポンシブ）。
- **CLI**: クラシック wizard と TUI (curses)。
- **Agent**: Web UI からプロセス起動。

## クイックスタート

```bash
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python scripts/smoke_local.py --reset --rounds 4 --workers 2 --device cpu
.venv/bin/python scripts/wizard.py
```

## ドキュメント

- [Onboarding](../ONBOARDING.md) — ゼロから最初のラウンドへ。
- [アルゴリズム](../01-algoritmo.md) · [アーキテクチャ](../02-arquitectura.md) · [通信](../03-comunicacion.md)
- [モデル](../04-modelos.md) · [学習](../05-entrenamiento.md) · [運用](../06-operaciones.md)
- [セキュリティ](../07-seguridad.md) · [実験](../08-experimentos.md) · [参考](../09-referencias.md)
- [Changelog](../../CHANGELOG.md) · [状態](../../STATUS.md) · [Roadmap](../../ROADMAP.md)