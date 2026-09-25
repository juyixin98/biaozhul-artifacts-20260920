#!/usr/bin/env python3
"""验收脚本：频偏扫描、SNR 扫描、相邻音切换、混淆矩阵与拒识统计。

仅针对合成音频场景。结果写入 results/ 目录（CSV + Markdown 摘要），
并打印到标准输出。运行：python scripts/run_acceptance.py
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dtmf.detector import DetectorConfig, DtmfDetector
from dtmf.synth import SynthConfig, synthesize, to_int16
from dtmf.tones import VALID_KEYS

RESULTS = Path(__file__).resolve().parent.parent / "results"
REJECT = "<REJECT>"


def classify(expected: str, decoded: str) -> str:
    """单键试验的分类结果：正确键 / 错误键 / REJECT。"""
    if decoded == expected:
        return expected
    if decoded == "":
        return REJECT
    return decoded  # 识别成了别的内容（混淆）


def run_trials(keys, synth_cfg, det_cfg, seed_base=0):
    """每个键合成一次并解码，返回 {expected: predicted} 列表。"""
    detector = DtmfDetector(det_cfg)
    outcomes = []
    for i, key in enumerate(keys):
        cfg = SynthConfig(**{**vars(synth_cfg), "seed": seed_base + i})
        signal, _ = synthesize(key, cfg)
        decoded = detector.decode(to_int16(signal)).digits
        outcomes.append((key, classify(key, decoded)))
    return outcomes


def accuracy(outcomes) -> float:
    return sum(1 for e, p in outcomes if e == p) / len(outcomes)


def reject_rate(outcomes) -> float:
    return sum(1 for _, p in outcomes if p == REJECT) / len(outcomes)


def sweep_freq_deviation(det_cfg):
    print("\n== 频偏扫描（SNR=30dB，每点 16 键）==")
    rows = []
    for dev in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
        out = run_trials(VALID_KEYS, SynthConfig(freq_deviation_pct=dev, snr_db=30.0),
                         det_cfg, seed_base=100)
        acc, rej = accuracy(out), reject_rate(out)
        rows.append((dev, acc, rej))
        print(f"  频偏 {dev:+.1f}% : 准确率 {acc*100:5.1f}%  拒识率 {rej*100:5.1f}%")
    return rows


def sweep_snr(det_cfg):
    print("\n== SNR 扫描（无频偏，每点 16 键）==")
    rows = []
    for snr in (40, 30, 20, 15, 10, 5, 0):
        out = run_trials(VALID_KEYS, SynthConfig(snr_db=float(snr)), det_cfg,
                         seed_base=200)
        acc, rej = accuracy(out), reject_rate(out)
        rows.append((snr, acc, rej))
        print(f"  SNR {snr:2d}dB : 准确率 {acc*100:5.1f}%  拒识率 {rej*100:5.1f}%")
    return rows


def sweep_adjacent_switch(det_cfg):
    print("\n== 相邻音切换（8 键随机序列，音长 60ms，SNR=30dB）==")
    print("  注：间隔 0ms 时相邻相同键与单个长音在信号上不可区分（真实 DTMF 同理），")
    print("      故 0ms 组序列不含相邻重复键；该限制在下方单独演示。")
    import random
    rows = []
    for gap_ms in (0, 10, 20, 40):
        rng = random.Random(7)
        total, ok = 0, 0
        for trial in range(10):
            while True:
                seq = "".join(rng.choice(VALID_KEYS) for _ in range(8))
                if gap_ms > 0 or all(a != b for a, b in zip(seq, seq[1:])):
                    break
            cfg = SynthConfig(gap_ms=float(gap_ms), tone_ms=60.0, snr_db=30.0,
                              seed=300 + trial)
            signal, _ = synthesize(seq, cfg)
            decoded = DtmfDetector(det_cfg).decode(to_int16(signal)).digits
            total += 1
            ok += decoded == seq
        rows.append((gap_ms, ok / total))
        print(f"  间隔 {gap_ms:2d}ms : 序列正确率 {ok}/{total}")
    # 已知限制演示：相邻相同键、零间隔 → 信号等价于单个长音
    cfg = SynthConfig(gap_ms=0.0, tone_ms=60.0)
    signal, _ = synthesize("00", cfg)
    merged = DtmfDetector(det_cfg).decode(to_int16(signal)).digits
    print(f"  已知限制：'00' 零间隔合成 -> 解码为 {merged!r}（合并为单键，无法避免）")
    return rows


def confusion_matrix(det_cfg):
    """在多个 SNR 条件下聚合全部单键试验，构建混淆矩阵。"""
    print("\n== 混淆矩阵（聚合 SNR=40/20/10dB × 16 键 × 3 次）==")
    matrix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for snr in (40, 20, 10):
        for rep in range(3):
            for key, pred in run_trials(VALID_KEYS, SynthConfig(snr_db=float(snr)),
                                        det_cfg, seed_base=400 + 10 * rep):
                matrix[key][pred] += 1
    labels = list(VALID_KEYS) + [REJECT]
    # 打印
    header = "true\\pred " + " ".join(f"{l:>9}" for l in labels)
    print("  " + header)
    lines = []
    for true in VALID_KEYS:
        row = [matrix[true].get(pred, 0) for pred in labels]
        lines.append("  " + f"{true:>9} " + " ".join(f"{v:9d}" for v in row))
        print(lines[-1])
    # 写 CSV
    RESULTS.mkdir(exist_ok=True)
    csv_path = RESULTS / "confusion_matrix.csv"
    with csv_path.open("w") as f:
        f.write("true," + ",".join(labels) + "\n")
        for true in VALID_KEYS:
            f.write(true + "," + ",".join(str(matrix[true].get(p, 0)) for p in labels) + "\n")
    total = sum(sum(r.values()) for r in matrix.values())
    correct = sum(matrix[k].get(k, 0) for k in VALID_KEYS)
    rejected = sum(matrix[k].get(REJECT, 0) for k in VALID_KEYS)
    print(f"  总计 {total} 次：正确 {correct}（{correct/total*100:.1f}%），"
          f"拒识 {rejected}（{rejected/total*100:.1f}%），"
          f"误识 {total-correct-rejected}（{(total-correct-rejected)/total*100:.1f}%）")
    print(f"  CSV 已写入 {csv_path}")
    return matrix, total, correct, rejected


def main():
    det_cfg = DetectorConfig()
    print("双音频率识别验收（合成音频场景，采样率 8000Hz，int16 PCM）")
    print(f"检测器配置: {det_cfg}")

    dev_rows = sweep_freq_deviation(det_cfg)
    snr_rows = sweep_snr(det_cfg)
    adj_rows = sweep_adjacent_switch(det_cfg)
    matrix, total, correct, rejected = confusion_matrix(det_cfg)

    RESULTS.mkdir(exist_ok=True)
    summary = RESULTS / "acceptance_summary.md"
    with summary.open("w") as f:
        f.write("# 验收结果摘要（自动生成）\n\n")
        f.write("场景：合成音频，8000Hz / int16 单声道，Goertzel 分帧检测。\n\n")
        f.write("## 频偏扫描（SNR=30dB）\n\n| 频偏 | 准确率 | 拒识率 |\n|---|---|---|\n")
        for dev, acc, rej in dev_rows:
            f.write(f"| {dev:+.1f}% | {acc*100:.1f}% | {rej*100:.1f}% |\n")
        f.write("\n## SNR 扫描（无频偏）\n\n| SNR | 准确率 | 拒识率 |\n|---|---|---|\n")
        for snr, acc, rej in snr_rows:
            f.write(f"| {snr}dB | {acc*100:.1f}% | {rej*100:.1f}% |\n")
        f.write("\n## 相邻音切换（8 键随机序列，音长 60ms，SNR=30dB）\n\n"
                "| 音间间隔 | 序列正确率 |\n|---|---|\n")
        for gap, acc in adj_rows:
            f.write(f"| {gap}ms | {acc*100:.0f}% |\n")
        f.write("\n注：0ms 组序列不含相邻重复键——相邻相同键零间隔时信号与单个长音"
                "完全等价，属原理性限制（真实 DTMF 同样要求音间静音）。")
        f.write(f"\n## 混淆矩阵\n\n聚合 {total} 次单键试验：正确 {correct}，"
                f"拒识 {rejected}，误识 {total-correct-rejected}。"
                f"明细见 confusion_matrix.csv。\n")
    print(f"\n摘要已写入 {summary}")


if __name__ == "__main__":
    main()
