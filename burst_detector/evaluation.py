"""检测效果评估：检测延迟、误报（误检点数/虚警率）、漏报。

针对带标签事件（阶跃起始点、漂移起点、孤立尖峰位置）评估：

- **检测延迟**：事件开始后第一个被判异常的样本与事件起点之差；
- **事件级命中/漏报**：在事件评估窗内出现异常判决即视为命中，
  否则漏报；
- **误报**：所有事件评估窗之外的异常判决点数，以及对干净信号的
  虚警率（异常点 / 可判决点数）。

事件评估窗的定义是显式的、只向事件之后开放（不回看过去），
与检测器本身的因果性一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .detector import Decision
from .processing import DetectionResult


@dataclass(frozen=True)
class LabeledEvent:
    """一个标注事件。

    kind: step / drift / spike 等自由标签
    start: 事件起始样本索引
    eval_end: 评估窗右边界（不含）；在 [start, eval_end) 内出现
              异常判决即算命中，延迟为首个异常点相对 start 的偏移
    """

    kind: str
    start: int
    eval_end: int

    def __post_init__(self) -> None:
        if self.eval_end <= self.start:
            raise ValueError(
                f"事件评估窗非法: start={self.start}, eval_end={self.eval_end}"
            )


@dataclass(frozen=True)
class EventScore:
    kind: str
    start: int
    eval_end: int
    detected: bool
    delay: int            # 未检出为 -1
    first_alarm: int      # 首次报警绝对索引；未检出为 -1


@dataclass(frozen=True)
class EvalReport:
    events: tuple[EventScore, ...]
    false_alarm_points: int           # 事件评估窗外的异常判决点数
    eligible_points: int              # 可判决点数（排除 warmup/missing）
    false_alarm_rate: float           # false_alarm_points / eligible_points
    n_detected: int
    n_missed: int
    delays: tuple[int, ...]           # 仅已检出事件的延迟
    mean_delay: float                 # 仅在有检出时定义，否则 NaN

    def summary_lines(self) -> list[str]:
        lines = [
            f"事件总数 {len(self.events)}，命中 {self.n_detected}，"
            f"漏报 {self.n_missed}",
        ]
        for e in self.events:
            status = (
                f"命中，延迟 {e.delay} 点 (首报 #{e.first_alarm})"
                if e.detected
                else "漏报"
            )
            lines.append(f"  - {e.kind} @ {e.start}: {status}")
        lines.append(
            f"误报点 {self.false_alarm_points} / 可判决点 "
            f"{self.eligible_points} = {self.false_alarm_rate:.6%}"
        )
        if self.delays:
            lines.append(
                f"已检出事件平均延迟 {self.mean_delay:.2f} 点，"
                f"各事件延迟 {list(self.delays)}"
            )
        return lines


def evaluate(
    result: DetectionResult,
    events,
    *,
    warmup_tolerance: int | None = None,
) -> EvalReport:
    """根据标注事件评估检测结果。

    warmup_tolerance: 落在预热区（前 ``warmup_tolerance`` 个样本，
        默认取所有 warmup 判决的长度）内的异常点不计误报，
        因为预热期检测器按设计不产出判决。
    """
    events = tuple(events)
    n = result.decisions.size
    alarm = result.is_anomaly.copy()

    alarm_in_window = np.zeros(n, dtype=bool)
    scores: list[EventScore] = []
    for ev in events:
        end = min(ev.eval_end, n)
        window = np.zeros(n, dtype=bool)
        window[ev.start : end] = True
        alarm_in_window |= window
        hits = np.flatnonzero(alarm & window)
        if hits.size:
            first = int(hits[0])
            scores.append(
                EventScore(
                    kind=ev.kind,
                    start=ev.start,
                    eval_end=end,
                    detected=True,
                    delay=first - ev.start,
                    first_alarm=first,
                )
            )
        else:
            scores.append(
                EventScore(
                    kind=ev.kind,
                    start=ev.start,
                    eval_end=end,
                    detected=False,
                    delay=-1,
                    first_alarm=-1,
                )
            )

    warmup_n = (
        int(result.warmup_indices.size)
        if warmup_tolerance is None
        else int(warmup_tolerance)
    )
    warmup_zone = np.zeros(n, dtype=bool)
    warmup_zone[: max(warmup_n, 0)] = True

    false_alarm = alarm & ~alarm_in_window & ~warmup_zone
    false_points = int(false_alarm.sum())
    judged = np.fromiter(
        (d is not Decision.MISSING for d in result.decisions),
        dtype=bool, count=n,
    )
    eligible = int((judged & ~warmup_zone).sum())
    rate = false_points / eligible if eligible else 0.0

    delays = tuple(e.delay for e in scores if e.detected)
    return EvalReport(
        events=tuple(scores),
        false_alarm_points=false_points,
        eligible_points=eligible,
        false_alarm_rate=rate,
        n_detected=len(delays),
        n_missed=len(scores) - len(delays),
        delays=delays,
        mean_delay=float(np.mean(delays)) if delays else float("nan"),
    )
