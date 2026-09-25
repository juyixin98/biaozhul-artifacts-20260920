"""pitjoin —— 离线特征时间点连接基础设施（NumPy + 标准库）。"""
from __future__ import annotations

from .engine import (
    Event,
    ExcludedRecord,
    FeatureRecord,
    FeatureStore,
    JoinConfig,
    JoinedFrame,
    REASON_ALL_FUTURE,
    REASON_ALL_LATE,
    REASON_DUPLICATE,
    REASON_MISSING_KEY,
    REASON_SELECTED,
    pit_join,
)
from .model import LogisticRegression, accuracy, chronological_split, train_logistic
from .synthetic import build_canonical_dataset, build_generated_dataset
from .times import dt, parse_iso, to_iso

__all__ = [
    "Event",
    "ExcludedRecord",
    "FeatureRecord",
    "FeatureStore",
    "JoinConfig",
    "JoinedFrame",
    "REASON_ALL_FUTURE",
    "REASON_ALL_LATE",
    "REASON_DUPLICATE",
    "REASON_MISSING_KEY",
    "REASON_SELECTED",
    "pit_join",
    "LogisticRegression",
    "accuracy",
    "chronological_split",
    "train_logistic",
    "build_canonical_dataset",
    "build_generated_dataset",
    "dt",
    "parse_iso",
    "to_iso",
]

__version__ = "0.1.0"
