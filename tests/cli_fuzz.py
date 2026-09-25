#!/usr/bin/env python3
"""CLI 随机 fuzz：生成随机请求 -> 调用二进制 -> 与独立参考逐格结果比对。"""
import json
import random
import subprocess
import sys

from oracle import grid_oracle


def main() -> int:
    bin_path = sys.argv[1]
    trials = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 20260924
    rng = random.Random(seed)

    for t in range(trials):
        n = rng.randrange(0, 12)
        rects = []
        for _ in range(n):
            x1 = rng.randrange(-10, 11)
            y1 = rng.randrange(-10, 11)
            x2 = rng.randrange(-10, 11)
            y2 = rng.randrange(-10, 11)
            rects.append([min(x1, x2), min(y1, y2),
                          max(x1, x2), max(y1, y2)])  # 含零面积退化
        if rects and rng.random() < 0.2:
            rects.append(list(rects[rng.randrange(len(rects))]))
        req = json.dumps({"rectangles": [
            {"x1": r[0], "y1": r[1], "x2": r[2], "y2": r[3]} for r in rects
        ]})
        proc = subprocess.run([bin_path], input=req, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"trial {t}: 非零退出 {proc.returncode}\n{proc.stderr}\n请求: {req}")
            return 1
        out = json.loads(proc.stdout)
        ga, gp = grid_oracle(rects)
        if int(out["area"]) != ga or int(out["perimeter"]) != gp:
            print(f"trial {t}: 分歧 CLI={out['area']},{out['perimeter']} "
                  f"oracle={ga},{gp}\n请求: {req}")
            return 1
    print(f"CLI fuzz {trials} 组全部一致（seed={seed}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
