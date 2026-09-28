"""Structured run logging with replay support.

Every execution (reuse / no-reuse, success / failure) appends JSONL records to
``logs/runs.jsonl``. A record carries the run id, UTC timestamp, key
intermediate state (wave transitions, live capacities) and the *reasoning* for
planner decisions / failures. Failed runs are never swallowed: they are logged
with their error category before the exception propagates.

Replay: :meth:`RunLogger.replay` loads all records of a run id in order; the
fixture scripts can rebuild exactly the graph/feed that triggered a problem
because inputs and shapes are logged too (values are synthetic and local).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .executor import RunTrace, now_iso


def _json_default(obj):
    # numpy scalar / ndarray -> plain python
    if hasattr(obj, "item"):
        return obj.item()
    if hasattr(obj, "tolist"):
        return obj.tolist()
    return str(obj)


class RunLogger:
    def __init__(self, log_dir: str | os.PathLike = "logs") -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / "runs.jsonl"
        self._lock = threading.Lock()

    def event(self, run_id: str, kind: str, **payload) -> dict:
        record = {
            "ts": now_iso(),
            "run_id": run_id,
            "kind": kind,
            **payload,
        }
        line = json.dumps(record, default=_json_default, sort_keys=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return record

    def log_inputs(
        self,
        run_id: str,
        graph_name: str,
        mode: str,
        feeds: dict,
        constants: dict | None,
        parallel: bool,
        plan_summary: dict | None,
    ) -> None:
        def describe(values):
            out = {}
            for name, arr in (values or {}).items():
                out[name] = {
                    "shape": list(arr.shape),
                    "dtype": str(arr.dtype),
                    # Synthetic local data: recording the full replay payload is
                    # safe and lets the exact problem be reconstructed.
                    "values": arr.tolist() if arr.size <= 64 else "<large:synthetic>",
                    "checksum": float(_kahan_sum(arr)),
                }
            return out

        self.event(
            run_id,
            "run_start",
            graph=graph_name,
            mode=mode,
            parallel=parallel,
            feeds=describe(feeds),
            constants=describe(constants),
            plan=plan_summary,
        )

    def log_trace(self, trace: RunTrace) -> None:
        self.event(
            trace.run_id,
            "run_trace",
            graph=trace.graph_name,
            mode=trace.mode,
            parallel=trace.parallel,
            status=trace.status,
            peak_capacity_bytes=trace.peak_capacity_bytes,
            peak_actual_bytes=trace.peak_actual_bytes,
            pinned_bytes=trace.pinned_bytes,
            output_shapes=trace.output_shapes,
            decisions=trace.decisions,
            waves=[
                {
                    "wave": w.wave,
                    "nodes": w.nodes,
                    "born": w.born,
                    "died": w.died,
                    "live_buffers": w.live_buffers,
                    "live_capacity_bytes": w.live_capacity_bytes,
                    "live_actual_bytes": w.live_actual_bytes,
                    "parallel": w.parallel,
                }
                for w in trace.waves
            ],
            failure=trace.failure,
        )

    def log_failure(self, run_id: str, category: str, message: str, details: dict, stage: str) -> None:
        self.event(
            run_id,
            "run_failure",
            stage=stage,
            category=category,
            message=message,
            details=details,
        )

    def replay(self, run_id: str) -> list[dict]:
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("run_id") == run_id:
                records.append(rec)
        return records

    def all_runs(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def latest_run_id(self) -> str | None:
        runs = self.all_runs()
        return runs[-1]["run_id"] if runs else None


def _kahan_sum(arr) -> float:
    """Order-insensitive-enough checksum for logging (not a correctness oracle)."""
    import numpy as np

    flat = np.asarray(arr, dtype=np.float64).ravel()
    total, comp = 0.0, 0.0
    for x in flat:
        y = float(x) - comp
        t = total + y
        comp = (t - total) - y
        total = t
    return total
