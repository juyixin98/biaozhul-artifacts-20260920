#!/usr/bin/env python3
"""多随机种子基准：误报率、检测延迟、漏报率（结果可复现）。

用法::

    python3 examples/benchmark.py            # 打印到 stdout
    python3 examples/benchmark.py > out/benchmark.txt
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from burst_detector.detector import DetectorConfig
from burst_detector.processing import detect_signal
from burst_detector.signal_io import add_drift, add_step, gaussian_noise

N = 2000
cfg = DetectorConfig()


def first_delay(x, onset, end=2000) -> int | None:
    a = detect_signal(x, cfg).anomaly_indices
    a = a[(a >= onset) & (a < end)]
    return int(a[0] - onset) if a.size else None


print("== 1) 干净高斯噪声误报（每场景 2000 点，50 个种子）==")
for t in (6.0, 8.0):
    c = DetectorConfig(threshold=t)
    total = 0
    worst = 0
    for seed in range(50):
        k = int(detect_signal(gaussian_noise(N, seed=seed), c).is_anomaly.sum())
        total += k
        worst = max(worst, k)
    judged = (N - c.min_samples) * 50
    print(f"  threshold={t}: 误报点 {total}/{judged} = {total/judged:.6%}, "
          f"单序列最多 {worst} 点")

print()
print("== 2) 阶跃检测延迟（onset=1000，10 个种子）==")
for amp in (6.0, 8.0):
    delays = [
        first_delay(add_step(gaussian_noise(N, seed=s), at=1000, amplitude=amp),
                    1000, 1200)
        for s in range(10)
    ]
    hit = [d for d in delays if d is not None]
    print(f"  amp={amp}σ: 命中 {len(hit)}/10，延迟 {sorted(hit)}，"
          f"平均 {np.mean(hit):.1f}")

print()
print("== 3) 孤立尖峰漏报（每序列 3 个尖峰 @600/1200/1700，50 个种子）==")
for amp in (8.0, 10.0, 12.0):
    miss = 0
    for seed in range(50):
        x = gaussian_noise(N, seed=seed).copy()
        for p in (600, 1200, 1700):
            x[p] += amp
        r = detect_signal(x, cfg)
        miss += sum(not r.is_anomaly[p] for p in (600, 1200, 1700))
    total_events = 50 * 3
    print(f"  amp={amp}σ: 命中 {total_events-miss}/{total_events}，"
          f"漏报率 {miss/total_events:.2%}（命中点延迟均为 0）")

print()
print("== 4) 线性漂移（onset=800，10 个种子，评估窗至 #1400）==")
for rate in (0.05, 0.12, 0.2):
    delays = [
        first_delay(add_drift(gaussian_noise(N, seed=s), at=800, rate=rate),
                    800, 1400)
        for s in range(10)
    ]
    hit = [d for d in delays if d is not None]
    if hit:
        print(f"  rate={rate}σ/点: 命中 {len(hit)}/10，延迟 {sorted(hit)}，"
              f"平均 {np.mean(hit):.1f}")
    else:
        print(f"  rate={rate}σ/点: 命中 0/10（慢漂移被滑动窗口自适应，"
              f"见 README 已知局限）")
