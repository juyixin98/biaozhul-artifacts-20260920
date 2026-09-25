"""sudden_anomaly: causal sliding robust-statistics pulse anomaly detector."""

from .detector import (
    DECISION_ANOMALY,
    DECISION_NORMAL,
    DECISION_NOT_DECIDED,
    DECISION_WARMING,
    DetectorConfig,
    DetectorConfigError,
    DetectionResult,
    SampleResult,
    StreamingAnomalyDetector,
    detect_offline,
)
from .signals import (
    EventTruth,
    SignalScenario,
    make_drift,
    make_missing,
    make_spikes,
    make_step,
)
from .evaluation import EvaluationReport, EventReport, evaluate
from .pcm_io import PcmInfo, read_pcm, write_json_summary, write_point_csv

__all__ = [
    "DECISION_ANOMALY",
    "DECISION_NORMAL",
    "DECISION_NOT_DECIDED",
    "DECISION_WARMING",
    "DetectorConfig",
    "DetectorConfigError",
    "DetectionResult",
    "SampleResult",
    "StreamingAnomalyDetector",
    "detect_offline",
    "EventTruth",
    "SignalScenario",
    "make_drift",
    "make_missing",
    "make_spikes",
    "make_step",
    "EvaluationReport",
    "EventReport",
    "evaluate",
    "PcmInfo",
    "read_pcm",
    "write_json_summary",
    "write_point_csv",
]
