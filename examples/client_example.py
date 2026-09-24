"""示例客户端：对运行中的服务执行一次完整的 仿真 -> 求解 -> 对比真值 流程。

用法：
    uvicorn main:app --port 8000   # 另一个终端先启动服务
    python examples/client_example.py [BASE_URL]
"""

from __future__ import annotations

import sys

import httpx
import numpy as np

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def main() -> None:
    # 1. 健康检查
    r = httpx.get(f"{BASE}/health", timeout=10)
    print("GET /health ->", r.json())

    # 2. 一键：生成带噪合成场景并用 Schur 补求解
    payload = {
        "scene": {
            "n_cameras": 5,
            "n_points": 40,
            "noise_px": 1.0,
            "seed": 0,
            "n_behind_camera": 2,
            "n_under_observed": 2,
        },
        "options": {"solver": "schur", "max_iterations": 50},
    }
    r = httpx.post(f"{BASE}/v1/solve_simulated", json=payload, timeout=60)
    r.raise_for_status()
    body = r.json()

    print("\nPOST /v1/solve_simulated (solver=schur)")
    print("  converged          :", body["converged"], f"({body['iterations']} 次迭代)")
    print("  初始代价           :", f"{body['cost_history'][0]:.4f}")
    print("  最终代价           :", f"{body['final_cost']:.6f}")
    diag = body["diagnostics"]
    print("  剔除的负深度观测   :", diag["num_negative_depth_excluded"])
    print("  观测不足的点       :", diag["under_observed_points"])
    sa = diag["scale_anchor"]
    print(f"  尺度锚定 ||t_1||   : target={sa['target']:.6f}  final={sa['final_t1_norm']:.6f}")

    # 3. 与真值对比（尺度已被锚定，可直接比较）
    gt_t1 = np.linalg.norm(np.array(body["ground_truth"]["cameras"][1]["tvec"]))
    est_t1 = np.linalg.norm(np.array(body["solution"]["cameras"][1]["tvec"]))
    print(f"  真值 ||t_1||       : {gt_t1:.6f}   估计: {est_t1:.6f}")

    # 4. 同一场景改用完整正规方程求解，验证两种方法一致
    sim = httpx.post(
        f"{BASE}/v1/simulate", json=payload["scene"], timeout=30
    ).json()
    costs = {}
    for solver in ("schur", "full"):
        r = httpx.post(
            f"{BASE}/v1/solve",
            json={"problem": sim["problem"], "options": {"solver": solver}},
            timeout=60,
        )
        costs[solver] = r.json()["final_cost"]
    print("\nPOST /v1/solve 两种求解器对比")
    print(f"  schur 最终代价: {costs['schur']:.10f}")
    print(f"  full  最终代价: {costs['full']:.10f}")
    print(f"  相对差异      : {abs(costs['schur'] - costs['full']) / costs['full']:.2e}")


if __name__ == "__main__":
    main()
