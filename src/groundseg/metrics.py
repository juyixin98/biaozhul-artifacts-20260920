"""与人工构造真值对比，计算地面点的精确率与召回率。

预测为三值（ground / non_ground / undecidable），真值为二值。
我们报告：

  precision          TP / (TP + FP)
                     预测地面中真正是地面的比例；undecidable 不计入预测地面。
  recall             TP / (TP + FN)
                     所有真实地面点中被成功标出的比例；漏判（含 undecidable）
                     算 FN。这是主召回率，体现“保守原则”的代价。
  decided_recall     仅在系统做出判定的真实地面点中统计的召回率
                     （剔除 undecidable），用于区分“判错”与“拒判”。
  undecided_rate     输出中 undecidable 的占数比例。

对 non_ground 类同样给出对称指标，便于完整查看。
"""

from __future__ import annotations

from dataclasses import dataclass

from .segment import GROUND, NON_GROUND, UNDECIDED


@dataclass(frozen=True)
class Metrics:
    target_label: str
    precision: float
    recall: float
    decided_recall: float
    specificity: float
    undecided_rate: float
    tp: int
    fp: int
    fn: int
    tn: int
    undecided: int
    total: int

    def as_dict(self) -> dict:
        d = {
            "target_label": self.target_label,
            "precision": self.precision,
            "recall": self.recall,
            "decided_recall": self.decided_recall,
            "specificity": self.specificity,
            "undecided_rate": self.undecided_rate,
            "tp": self.tp, "fp": self.fp,
            "fn": self.fn, "tn": self.tn,
            "undecided": self.undecided, "total": self.total,
        }
        d["confusion"] = {
            "tp": self.tp, "fp": self.fp,
            "fn": self.fn, "tn": self.tn,
            "undecided": self.undecided, "total": self.total,
        }
        return d


def _metrics_for(target: str, predicted: list[str], truth: list[str | int | bool]) -> Metrics:
    def is_target(t) -> bool:
        if isinstance(t, bool):
            return t if target == GROUND else (not t)
        if isinstance(t, int):
            return (t == 1) if target == GROUND else (t == 0)
        if isinstance(t, str):
            return t == target
        # 浮点真值等：非零即正类
        return bool(t) if target == GROUND else not bool(t)

    tp = fp = fn = tn = und = 0
    for pred, gt in zip(predicted, truth):
        actual_pos = is_target(gt)
        if pred == UNDECIDED:
            und += 1
            if actual_pos:
                fn += 1   # 拒判正类：召回率算漏
            else:
                tn += 1   # 拒判负类：不冤枉，记为真负但单列
            continue
        pred_pos = pred == target
        if pred_pos and actual_pos:
            tp += 1
        elif pred_pos and not actual_pos:
            fp += 1
        elif not pred_pos and actual_pos:
            fn += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    # decided recall：实际正类中被系统做出判定（非 undecided）的样本里，
    # 判对的比例。判错（正类判成另一确定标签）与拒判（undecided）区分开。
    decided_pos = sum(
        1 for pred, gt in zip(predicted, truth)
        if pred != UNDECIDED and is_target(gt)
    )
    decided_recall = tp / decided_pos if decided_pos else 0.0
    specificity = tn / (tn + fp) if (tn + fp) else 0.0
    total = len(predicted)
    return Metrics(
        target_label=target,
        precision=precision,
        recall=recall,
        decided_recall=decided_recall,
        specificity=specificity,
        undecided_rate=und / total if total else 0.0,
        tp=tp, fp=fp, fn=fn, tn=tn, undecided=und, total=total,
    )


def evaluate(predicted: list[str] | SegmentResultLike, truth: list) -> dict:
    """计算完整指标。入参可以是标签列表或 SegmentResult。"""
    if hasattr(predicted, "labels"):
        pred_labels = predicted.labels
    else:
        pred_labels = list(predicted)
    truth = list(truth)
    if len(pred_labels) != len(truth):
        raise ValueError(
            f"预测长度 {len(pred_labels)} 与真值长度 {len(truth)} 不一致"
        )
    ground = _metrics_for(GROUND, pred_labels, truth)
    non_ground = _metrics_for(NON_GROUND, pred_labels, truth)
    return {
        "ground": ground.as_dict(),
        "non_ground": non_ground.as_dict(),
        "summary": {
            "total": ground.total,
            "ground_truth_positive": ground.tp + ground.fn,
            "predicted_ground": ground.tp + ground.fp,
            "undecided": ground.undecided,
        },
    }


# 仅用于类型提示的轻量协议（避免循环导入 SegmentResult）。
class SegmentResultLike:  # pragma: no cover - 纯文档
    labels: list[str]
