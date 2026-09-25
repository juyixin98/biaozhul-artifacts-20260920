"""输出层：把逐点检测结果写成数值文件（CSV/JSON/标记 PCM），不做任何界面。

文件产物：
- ``*_points.csv``  逐点明细（index,value,zscore,median,scale,decision,n_history）
- ``*_summary.json`` 汇总（配置、异常索引、缺样/预热索引、可选评估指标）
- ``*_marks.pcm``    可选：与输入等长的 uint8 标记流（1=异常，0=其他）
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

from .detector import Decision, DetectorConfig
from .evaluation import EvalReport
from .processing import DetectionResult

DECISION_CODE = {
    Decision.WARMUP: 0,
    Decision.MISSING: 1,
    Decision.NORMAL: 2,
    Decision.ANOMALY: 3,
}
CODE_DECISION = {v: k for k, v in DECISION_CODE.items()}


def write_points_csv(path: str | Path, result: DetectionResult) -> str:
    """写逐点 CSV。NaN 写成空字符串，保证可被标准工具读回。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def num(v: float) -> str:
        return "" if not np.isfinite(v) else f"{v:.12g}"

    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["index", "value", "zscore", "median", "scale", "decision", "n_history"]
        )
        for i in range(result.decisions.size):
            w.writerow(
                [
                    i,
                    num(result.values[i]),
                    num(result.zscores[i]),
                    num(result.medians[i]),
                    num(result.scales[i]),
                    result.decisions[i].value,
                    int(result.n_history[i]),
                ]
            )
    return str(path)


def write_marks_pcm(path: str | Path, result: DetectionResult) -> str:
    """写等长 uint8 标记 PCM：异常点=1，其余=0。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    marks = result.is_anomaly.astype(np.uint8)
    marks.tofile(path)
    return str(path)


def build_summary(
    result: DetectionResult,
    config: DetectorConfig,
    *,
    source: str | None = None,
    eval_report: EvalReport | None = None,
) -> dict:
    """构造可 JSON 序列化的汇总字典。"""
    n = result.decisions.size
    summary = {
        "source": source,
        "n_samples": int(n),
        "config": {
            "window_size": config.window_size,
            "threshold": config.threshold,
            "min_samples": config.min_samples,
            "min_scale": config.min_scale,
            "min_duration": config.min_duration,
            "missing_policy": config.missing_policy.value,
        },
        "counts": {
            "anomaly": int(result.is_anomaly.sum()),
            "missing": int(result.missing_indices.size),
            "warmup": int(result.warmup_indices.size),
            "normal": int(
                np.fromiter(
                    (d is Decision.NORMAL for d in result.decisions),
                    dtype=bool, count=result.decisions.size,
                ).sum()
            ),
        },
        "anomaly_indices": [int(i) for i in result.anomaly_indices],
        "missing_indices": [int(i) for i in result.missing_indices],
    }
    if eval_report is not None:
        summary["evaluation"] = {
            "n_events": len(eval_report.events),
            "n_detected": eval_report.n_detected,
            "n_missed": eval_report.n_missed,
            "delays": list(eval_report.delays),
            "mean_delay": (
                None
                if not np.isfinite(eval_report.mean_delay)
                else eval_report.mean_delay
            ),
            "false_alarm_points": eval_report.false_alarm_points,
            "eligible_points": eval_report.eligible_points,
            "false_alarm_rate": eval_report.false_alarm_rate,
        }
    return summary


def write_summary_json(
    path: str | Path,
    result: DetectionResult,
    config: DetectorConfig,
    *,
    source: str | None = None,
    eval_report: EvalReport | None = None,
) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = build_summary(
        result, config, source=source, eval_report=eval_report
    )
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    return str(path)
