"""本地示例：对三个代表性多项式调用求解服务。

前置：先在另一终端启动服务
    .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000

用法：
    .venv/bin/python examples/client_example.py
"""

from __future__ import annotations

import json
import os

import httpx

BASE = os.environ.get("POLYROOTS_BASE_URL", "http://127.0.0.1:8000")


def show(title: str, payload: dict) -> None:
    print("=" * 72)
    print(title)
    r = httpx.post(f"{BASE}/api/v1/roots", json=payload, timeout=30)
    print(f"HTTP {r.status_code}")
    body = r.json()
    if r.status_code != 200:
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return
    print(f"run_id={body['run_id']}  status={body['status']}  "
          f"degree={body['degree']}  kernel={body['kernel']['name']}")
    for root in body["roots"]:
        print(f"  z[{root['index']}] = {root['real']:+.10f}{root['imag']:+.2e}j"
              f"  kind={root['kind']:<14} res={root['relative_residual']:.2e}"
              f"  κ={root['sensitivity_indicator']:.2e}"
              f"  converged={root['converged']}")
    fe = body["factor_error"]
    print(f"  factor: robust={fe['max_rel_coeff_error']:.2e} "
          f"strict64={fe['strict_float64_error']:.2e} "
          f"highprec={fe['high_precision_error']}")
    print(f"  vieta : sum={body['vieta']['sum_rel_error']:.2e} "
          f"product={body['vieta']['product_rel_error']:.2e}")
    for w in body["warnings"]:
        print(f"  WARNING: {w}")


def main() -> None:
    # 1) 已知复根（含共轭对）：x^2+1 -> 降序 [1,0,1]
    show("x^2 + 1（共轭对）", {"coefficients": [1, 0, 1], "run_id": "demo-x2p1"})

    # 2) 近重根 (x-1)(x-1-1e-7)(x+4)(x-0.5)：演示“小残差不等于准确”
    # 降序系数（由根独立展开）：
    #   x^4 + 1.4999999 x^3 - 8.00000025 x^2 + 7.50000055 x - 2.0000002
    show("近重根多项式（间距 1e-7）", {
        "coefficients": [
            1.0,
            1.4999998999999997,
            -8.000000250000001,
            7.500000550000001,
            -2.0000002,
        ],
        "run_id": "demo-near",
    })

    # 3) 高阶稀疏 x^64 - 1：演示 float64 重构消去与高精度复核
    coeffs = [0.0] * 65
    coeffs[0] = 1.0   # x^64
    coeffs[-1] = -1.0  # -1
    show("x^64 - 1（高阶稀疏）", {
        "coefficients": coeffs, "kernel": "aberth", "max_iterations": 300,
        "run_id": "demo-unity64",
    })


if __name__ == "__main__":
    main()
