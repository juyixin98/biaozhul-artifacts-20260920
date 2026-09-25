"""Offline evaluation metrics: per-event detection delay and false alarms."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from .detector import (
    DECISION_ANOMALY,
    DECISION_WARMING,
    DECISION_NOT_DECIDED,
    DetectionResult,
)
from .signals import SignalScenario


@dataclass(frozen=True)
class EventReport:
    kind: str
    start: int
    end: int
    amplitude: float
    detected: bool
    detection_index: Optional[int]
    delay_samples: Optional[int]
    delay_seconds: Optional[float]


@dataclass(frozen=True)
class EvaluationReport:
    scenario: str
    sample_rate: float
    n_samples: int
    n_eligible: int  # samples that could be flagged (post warm-up, observed)
    n_anomalies: int
    events: List[EventReport] = field(default_factory=list)
    n_false_alarms: int = 0
    false_alarm_indices: List[int] = field(default_factory=list)
    false_alarm_rate_per_1000: float = 0.0
    detection_rate: float = 0.0
    mean_delay_samples: Optional[float] = None
    mean_delay_seconds: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "scenario": self.scenario,
            "sample_rate": self.sample_rate,
            "n_samples": self.n_samples,
            "n_eligible": self.n_eligible,
            "n_anomalies": self.n_anomalies,
            "events": [
                {
                    "kind": e.kind,
                    "start": e.start,
                    "end": e.end,
                    "amplitude": e.amplitude,
                    "detected": e.detected,
                    "detection_index": e.detection_index,
                    "delay_samples": e.delay_samples,
                    "delay_seconds": e.delay_seconds,
                }
                for e in self.events
            ],
            "n_false_alarms": self.n_false_alarms,
            "false_alarm_indices": self.false_alarm_indices,
            "false_alarm_rate_per_1000": self.false_alarm_rate_per_1000,
            "detection_rate": self.detection_rate,
            "mean_delay_samples": self.mean_delay_samples,
            "mean_delay_seconds": self.mean_delay_seconds,
        }


def evaluate(
    scenario: SignalScenario,
    result: DetectionResult,
    *,
    search_horizon: Optional[int] = None,
) -> EvaluationReport:
    """Match anomaly decisions against ground-truth events.

    Detection rule for an event ``[start, end]``: the first ANOMALY decision at
    an index in ``[start, min(end, start + search_horizon)]`` is the detection;
    its delay is ``index - start``. Any ANOMALY outside every ground-truth
    region is a false alarm (WARMING / NOT_DECIDED are never counted).
    """
    decisions = result.decisions
    n = scenario.n_samples
    if decisions.size != n:
        raise ValueError("decision length does not match scenario length")

    anomaly_positions = set(np.flatnonzero(decisions == DECISION_ANOMALY).tolist())
    truth_mask = scenario.anomaly_mask()

    false_idx = sorted(i for i in anomaly_positions if not truth_mask[i])
    eligible = int(
        np.count_nonzero(
            (decisions != DECISION_WARMING) & (decisions != DECISION_NOT_DECIDED)
        )
    )

    events: List[EventReport] = []
    delays: List[int] = []
    for ev in scenario.truth:
        last = ev.end if search_horizon is None else min(
            ev.end, ev.start + search_horizon
        )
        hits = [i for i in range(ev.start, last + 1) if i in anomaly_positions]
        if hits:
            det = hits[0]
            delay = det - ev.start
            delays.append(delay)
            events.append(
                EventReport(
                    ev.kind, ev.start, ev.end, ev.amplitude, True,
                    det, delay, delay / scenario.sample_rate,
                )
            )
        else:
            events.append(
                EventReport(
                    ev.kind, ev.start, ev.end, ev.amplitude, False,
                    None, None, None,
                )
            )

    n_events = len(events)
    n_detected = len(delays)
    return EvaluationReport(
        scenario=scenario.name,
        sample_rate=scenario.sample_rate,
        n_samples=n,
        n_eligible=eligible,
        n_anomalies=int(len(anomaly_positions)),
        events=events,
        n_false_alarms=len(false_idx),
        false_alarm_indices=false_idx,
        false_alarm_rate_per_1000=(1000.0 * len(false_idx) / eligible if eligible else 0.0),
        detection_rate=(n_detected / n_events if n_events else 0.0),
        mean_delay_samples=(float(np.mean(delays)) if delays else None),
        mean_delay_seconds=(
            float(np.mean(delays)) / scenario.sample_rate if delays else None
        ),
    )
