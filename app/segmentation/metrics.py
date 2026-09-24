"""Evaluation metrics against constructed ground-truth labels."""

from __future__ import annotations

from dataclasses import dataclass

LABEL_GROUND = "ground"
LABEL_NON_GROUND = "non_ground"
LABEL_UNKNOWN = "unknown"


@dataclass(frozen=True)
class MetricSet:
    precision: float
    recall: float
    f1: float
    tp: int
    fp: int
    fn: int
    tn: int


@dataclass(frozen=True)
class EvaluationReport:
    ground: MetricSet          # "ground" treated as the positive class
    non_ground: MetricSet      # "non_ground" treated as the positive class
    accuracy: float            # over points with a definite predicted label
    total_points: int
    unknown_predictions: int
    unknown_ground_truth: int  # ground-truth unknowns are excluded from scoring
    label_distribution: dict[str, int]


def evaluate(
    predicted: list[str],
    truth: list[str],
    positive: str = LABEL_GROUND,
) -> MetricSet:
    """Binary precision/recall/F1 for one positive label.

    Points whose *ground truth* is ``unknown`` are excluded entirely.  On
    known-truth points, a predicted ``unknown`` is an abstention: it counts as
    a false negative from the positive class's perspective and as a false
    positive from the negative class's perspective (visible when this function
    is called with the other label as ``positive``).  It is never silently
    treated as a success.
    """
    negative = LABEL_NON_GROUND if positive == LABEL_GROUND else LABEL_GROUND
    tp = fp = fn = tn = 0
    for pred, gold in zip(predicted, truth):
        if gold not in (positive, negative):
            continue
        if pred == positive:
            tp += gold == positive
            fp += gold == negative
        elif pred == negative:
            tn += gold == negative
            fn += gold == positive
        else:  # predicted unknown -> abstention
            fn += gold == positive
            fp += gold == negative
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return MetricSet(float(precision), float(recall), float(f1),
                     int(tp), int(fp), int(fn), int(tn))


def evaluate_report(predicted: list[str], truth: list[str]) -> EvaluationReport:
    """Full two-sided report plus unknown-bucket accounting."""
    g = evaluate(predicted, truth, LABEL_GROUND)
    ng = evaluate(predicted, truth, LABEL_NON_GROUND)

    scored = [
        (p, t)
        for p, t in zip(predicted, truth)
        if t in (LABEL_GROUND, LABEL_NON_GROUND)
    ]
    correct = sum(1 for p, t in scored if p == t)
    accuracy = correct / len(scored) if scored else 0.0

    dist: dict[str, int] = {}
    for label in predicted:
        dist[label] = dist.get(label, 0) + 1

    return EvaluationReport(
        ground=g,
        non_ground=ng,
        accuracy=accuracy,
        total_points=len(truth),
        unknown_predictions=sum(1 for p in predicted if p == LABEL_UNKNOWN),
        unknown_ground_truth=sum(
            1 for t in truth if t not in (LABEL_GROUND, LABEL_NON_GROUND)
        ),
        label_distribution=dist,
    )
