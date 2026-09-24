#!/usr/bin/env python3
"""Leakage demo: train a tiny NumPy logistic regression on features built by
(a) the correct point-in-time join and (b) a leaky join that ignores
ingest_ts, on fully synthetic, reproducible data (fixed seed, no downloads).

Story: half of the feature rows are later "revised" by a backfill job that
accidentally used the label (value = label exactly). The leaky join picks
those revisions and reports near-perfect accuracy; the point-in-time join
only sees versions that existed at the spine time and reports the honest
accuracy of the true signal.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from pitjoin import FeatureRecord, FeatureStore, SpineRow, join_as_matrix
from pitjoin.reference import naive_join

SEED = 20260922
N_EVENTS = 4000
SIGNAL_NOISE = 0.8  # label noise scale; honest accuracy ceiling ~0.79
REVISED_FRACTION = 0.5
INGEST_LAG_ORIGINAL = 5
INGEST_LAG_REVISION = 100


def build_synthetic() -> tuple[list[FeatureRecord], list[SpineRow], np.ndarray]:
    rng = np.random.default_rng(SEED)
    n_entities = 200
    x = rng.normal(0.0, 1.0, N_EVENTS)
    logits = x + rng.normal(0.0, SIGNAL_NOISE, N_EVENTS)
    y = (logits > 0).astype(np.float64)

    records: list[FeatureRecord] = []
    spine: list[SpineRow] = []
    revised = rng.random(N_EVENTS) < REVISED_FRACTION
    for i in range(N_EVENTS):
        entity = f"u{i % n_entities}"
        t = 1000 + i  # one event per time unit keeps times distinct
        # Spine time sits between the original ingest (t+5) and the leaky
        # revision (t+100): the honest join sees only the original value.
        spine.append(SpineRow(entity, t + 10))
        records.append(
            FeatureRecord(entity, "signal", float(x[i]), event_ts=t, ingest_ts=t + INGEST_LAG_ORIGINAL)
        )
        if revised[i]:
            # Post-hoc revision of the SAME event time, ingested much later,
            # whose value encodes the label (the leakage source).
            records.append(
                FeatureRecord(
                    entity, "signal", float(y[i]),
                    event_ts=t, ingest_ts=t + INGEST_LAG_REVISION,
                )
            )
    return records, spine, y


def train_logistic_regression(x: np.ndarray, y: np.ndarray, epochs: int = 300, lr: float = 0.5) -> np.ndarray:
    w = np.zeros(x.shape[1], dtype=np.float64)
    for _ in range(epochs):
        p = 1.0 / (1.0 + np.exp(-x @ w))
        w -= lr * (x.T @ (p - y)) / len(y)
    return w


def accuracy(features: np.ndarray, y: np.ndarray) -> float:
    split = int(0.7 * len(y))
    x_train = np.column_stack([np.ones(split), features[:split]])
    x_test = np.column_stack([np.ones(len(y) - split), features[split:]])
    w = train_logistic_regression(x_train, y[:split])
    pred = (x_test @ w > 0).astype(np.float64)
    return float(np.mean(pred == y[split:]))


def main() -> int:
    records, spine, y = build_synthetic()

    store = FeatureStore()
    store.ingest(records)
    pit_matrix, missing, _ = join_as_matrix(store, spine, ["signal"])
    assert not missing.any(), "demo spine should have full feature coverage"

    leaky_selected = naive_join(records, spine, ["signal"], ignore_ingest_ts=True)
    leaky_matrix = np.array(
        [[np.nan if rec is None else rec.value for rec in row] for row in leaky_selected],
        dtype=np.float64,
    )
    assert not np.isnan(leaky_matrix).any()

    acc_pit = accuracy(pit_matrix, y)
    acc_leaky = accuracy(leaky_matrix, y)

    print("=" * 72)
    print(f"synthetic data: {N_EVENTS} events, seed={SEED}, "
          f"{sum(1 for r in records if r.ingest_ts - r.event_ts == INGEST_LAG_REVISION)} label-encoding revisions")
    print(f"honest  (point-in-time join): test accuracy = {acc_pit:.4f}")
    print(f"leaky   (ignores ingest_ts ): test accuracy = {acc_leaky:.4f}")
    print(f"inflation from leakage:       +{acc_leaky - acc_pit:.4f}")
    print("=" * 72)
    if acc_leaky <= acc_pit + 0.05:
        print("RESULT: FAIL (expected leaky accuracy to be clearly inflated)")
        return 1
    print("RESULT: PASS (point-in-time join avoids the inflated, leaky accuracy)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
