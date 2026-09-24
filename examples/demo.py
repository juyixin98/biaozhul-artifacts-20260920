"""验收示例: 通过 HTTP 调用运行中的服务, 覆盖验收要求的全部场景。

前置: 服务已启动 (uvicorn app.main:app --port 8000)
运行: python examples/demo.py
"""

import math
import sys

import httpx
import numpy as np

BASE = "http://127.0.0.1:8017"


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=10.0)
    failures = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"[{mark}] {name}" + (f"  {detail}" if detail else ""))

    # 1. FK -> IK 回代: 末端误差
    rng = np.random.default_rng(2026)
    max_err = 0.0
    for _ in range(200):
        t1, t2 = rng.uniform(-math.pi, math.pi, size=2)
        pos = client.post("/fk", json={"theta1": t1, "theta2": t2}).json()
        ik = client.post("/ik", json={"x": pos["x"], "y": pos["y"]}).json()
        for s in ik["solutions"]:
            p = client.post("/fk", json={"theta1": s["theta1"], "theta2": s["theta2"]}).json()
            max_err = max(max_err, math.hypot(p["x"] - pos["x"], p["y"] - pos["y"]))
    check("FK->IK 回代末端误差 (200 随机样本)", max_err < 1e-9, f"max_err={max_err:.3e}")

    # 2. 肘部翻转: 同一目标两支解
    pos = client.post("/fk", json={"theta1": 0.3, "theta2": 0.8}).json()
    ik = client.post("/ik", json={"x": pos["x"], "y": pos["y"]}).json()
    branches = {s["branch"]: s for s in ik["solutions"]}
    ok = (
        len(branches) == 2
        and branches["elbow_down"]["theta2"] > 0 > branches["elbow_up"]["theta2"]
    )
    check("肘部翻转两支解 (elbow_down/elbow_up)", ok,
          f"theta2_down={branches['elbow_down']['theta2']:.4f}, "
          f"theta2_up={branches['elbow_up']['theta2']:.4f}")

    # 3. 完全伸直: 奇异
    ik = client.post("/ik", json={"x": 2.0, "y": 0.0}).json()
    check("完全伸直识别为 singular", ik["status"] == "singular",
          f"status={ik['status']}, solutions={len(ik['solutions'])}")

    # 4. 不可达: 不裁剪坐标
    ik = client.post("/ik", json={"x": 3.0, "y": 0.0}).json()
    check("超程目标如实报 unreachable", ik["status"] == "unreachable"
          and ik["solutions"] == [])

    # 5. 限位冲突
    lim = {"theta1_min": -math.pi / 2, "theta1_max": math.pi / 2,
           "theta2_min": -math.pi / 2, "theta2_max": math.pi / 2}
    ik = client.post("/ik", json={"x": -0.9, "y": 0.9,
                                  "arm": {"l1": 1.0, "l2": 1.0, "limits": lim}}).json()
    check("限位冲突如实报 limit_violation", ik["status"] == "limit_violation",
          f"within_limits={[s['within_limits'] for s in ik['solutions']]}")

    # 6. 路径连续选解: 关节跳变有界且不翻肘
    pts = []
    for t1, t2 in zip(np.linspace(-1, 1, 50), np.linspace(0.5, 1.2, 50)):
        p = client.post("/fk", json={"theta1": float(t1), "theta2": float(t2)}).json()
        pts.append([p["x"], p["y"]])
    steps = client.post("/ik/path", json={"points": pts}).json()["steps"]
    jumps = [s["step_jump"] for s in steps if s["step_jump"] is not None]
    branches_used = {s["branch"] for s in steps}
    check("路径连续选解 (50 点)", all(s["status"] == "ok" for s in steps)
          and branches_used == {"elbow_down"} and max(jumps) < 0.2,
          f"max_step_jump={max(jumps):.4f} rad, branches={branches_used}")

    print(f"\n{'全部通过' if failures == 0 else f'{failures} 项失败'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
