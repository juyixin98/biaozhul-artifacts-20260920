"""端到端演示:合成数据 + 简单模型 + 校准评估。

运行: python3 examples/run_demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calibration_eval.metrics import evaluate
from calibration_eval.synthetic import (
    make_synthetic,
    sigmoid,
    temperature_scale,
    train_logistic_regression,
)


def main() -> None:
    X, y, p_true = make_synthetic(n_samples=2000, n_features=5)
    w, b = train_logistic_regression(X, y)
    p_model = sigmoid(X @ w + b)
    p_miscal = temperature_scale(p_model, temperature=3.0)

    for name, p in [
        ("真实概率(完美校准参照)", p_true),
        ("逻辑回归模型", p_model),
        ("温度缩放 T=3(刻意失准)", p_miscal),
    ]:
        r = evaluate(y, p, n_bins=10)
        print(f"\n=== {name} ===")
        print(
            f"  Brier = {r['brier_score']:.6f}  "
            f"LogLoss = {r['log_loss']:.6f}  "
            f"ECE = {r['ece']:.6f}"
        )

    # 带样本权重的调用示例
    weights = [1.0] * 1000 + [3.0] * 1000
    r = evaluate(y, p_model, sample_weight=weights, n_bins=10)
    print("\n=== 加权示例(后 1000 条权重为 3) ===")
    print(json.dumps({k: v for k, v in r.items() if k != "bins"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
