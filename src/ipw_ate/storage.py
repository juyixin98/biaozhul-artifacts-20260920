"""SQLite persistence of *aggregate* run records (never row-level data).

Only the request id, decision, reasons and non-identifying summary statistics
are stored. This gives the diagnostic a durable, auditable trail without
persisting sensitive covariates/outcomes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .contract import DiagnosticResult, EstimateResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    request_id   TEXT PRIMARY KEY,
    estimand     TEXT NOT NULL,
    decision     TEXT NOT NULL,
    reasons      TEXT NOT NULL,
    n            INTEGER NOT NULL,
    n_treated    INTEGER NOT NULL,
    n_control    INTEGER NOT NULL,
    n_splits     INTEGER NOT NULL,
    ess_treated  REAL NOT NULL,
    ess_control  REAL NOT NULL,
    score_min    REAL NOT NULL,
    score_max    REAL NOT NULL,
    max_weight   REAL NOT NULL,
    max_balance_z REAL NOT NULL,
    single_arm_cells INTEGER NOT NULL,
    estimate     REAL,
    std_error    REAL,
    message      TEXT NOT NULL,
    created_utc  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


class RunStore:
    """Thin sqlite wrapper; one file, opened per call (safe for local use)."""

    def __init__(self, path: str | Path = "data/runs.db") -> None:
        self.path = str(path)
        parent = Path(self.path).parent
        parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def record_result(self, result: EstimateResult) -> None:
        d = result.diagnostic
        self._write(
            d,
            estimate=float(result.estimate),
            std_error=float(result.std_error),
        )

    def record_diagnostic(self, d: DiagnosticResult, error_code: str) -> None:
        self._write(d, estimate=None, std_error=None, error_code=error_code)

    def _write(
        self,
        d: DiagnosticResult,
        *,
        estimate: float | None,
        std_error: float | None,
        error_code: str | None = None,
    ) -> None:
        message = d.message if error_code is None else f"{error_code}: {d.message}"
        with self._connect() as con:
            con.execute(
                """INSERT OR REPLACE INTO runs(request_id, estimand, decision,
                   reasons, n, n_treated, n_control, n_splits, ess_treated,
                   ess_control, score_min, score_max, max_weight,
                   max_balance_z, single_arm_cells, estimate, std_error,
                   message)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    d.request_id,
                    d.estimand,
                    d.decision.value,
                    json.dumps(list(d.reasons)),
                    d.n,
                    d.n_treated,
                    d.n_control,
                    d.n_splits,
                    float(d.ess_treated),
                    float(d.ess_control),
                    float(d.score_min),
                    float(d.score_max),
                    float(d.max_weight),
                    float(d.max_balance_z),
                    int(d.single_arm_cells),
                    estimate,
                    std_error,
                    message,
                ),
            )

    def get(self, request_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM runs WHERE request_id = ?", (request_id,)
            ).fetchone()
        return dict(row) if row else None
