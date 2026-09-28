"""Evidence store: SQLite persistence of estimation runs and decisions.

Every accepted AND rejected estimation is recorded with its request id,
verdict, and the *key state* behind the verdict. Observation-level data is
never stored -- only dimensions, statistics, and the caller's assumption
claim. The store is intentionally dependency-free (stdlib sqlite3) and safe
to use from the local demo and the service.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    request_id      TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    nobs            INTEGER NOT NULL,
    k_endog         INTEGER NOT NULL,
    m_exog          INTEGER NOT NULL,
    L_instruments   INTEGER NOT NULL,
    covariance      TEXT NOT NULL,
    status          TEXT NOT NULL,
    error_code      TEXT,
    key_state       TEXT NOT NULL,
    warnings        TEXT NOT NULL,
    exclusion_asserted INTEGER NOT NULL,
    exclusion_rationale TEXT
);
CREATE TABLE IF NOT EXISTS coefficients (
    request_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    estimate    REAL NOT NULL,
    std_error   REAL NOT NULL,
    p_value     REAL NOT NULL,
    PRIMARY KEY (request_id, name),
    FOREIGN KEY (request_id) REFERENCES runs(request_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EvidenceStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def record_outcome(self, outcome) -> None:
        ident = outcome.identification
        key_state = {
            "status": outcome.status,
            "rank": ident.rank_value,
            "rank_required": ident.rank_required,
            "order_condition": ident.order_condition,
            "rank_condition": ident.rank_condition,
            "cragg_donald": ident.cragg_donald_statistic,
            "min_first_stage_f": min((f.effective_f_statistic for f in outcome.first_stage), default=None),
            "min_partial_r2": min((f.partial_r2 for f in outcome.first_stage), default=None),
            "sargan": {
                "testable": outcome.overidentification.testable,
                "statistic": outcome.overidentification.statistic,
                "p_value": outcome.overidentification.p_value,
                "verdict": outcome.overidentification.verdict,
            },
            "endogeneity": {
                "statistic": outcome.endogeneity.statistic,
                "p_value": outcome.endogeneity.p_value,
                "verdict": outcome.endogeneity.verdict,
            },
        }
        exclusion = outcome.assumptions["exclusion_restriction"]
        with self._connect() as con:
            con.execute(
                """INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    outcome.request_id,
                    _now(),
                    outcome.nobs,
                    outcome.n_endog,
                    outcome.n_exog,
                    outcome.n_instruments,
                    outcome.covariance_kind,
                    outcome.status,
                    None,
                    json.dumps(key_state, default=_json_default),
                    json.dumps(list(outcome.warnings)),
                    1 if exclusion["asserted_by_caller"] else 0,
                    exclusion.get("rationale", ""),
                ),
            )
            con.execute("DELETE FROM coefficients WHERE request_id = ?", (outcome.request_id,))
            con.executemany(
                "INSERT INTO coefficients VALUES (?,?,?,?,?)",
                [
                    (
                        outcome.request_id,
                        c.name,
                        c.estimate,
                        c.std_error,
                        c.p_value,
                    )
                    for c in outcome.coefficients
                ],
            )

    def record_error(self, request_id: str, error_code: str, key_state: Mapping[str, Any]) -> None:
        state = dict(key_state)
        with self._connect() as con:
            con.execute(
                """INSERT INTO runs (request_id, created_at, nobs, k_endog, m_exog,
                   L_instruments, covariance, status, error_code, key_state, warnings,
                   exclusion_asserted, exclusion_rationale)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    request_id,
                    _now(),
                    int(state.get("nobs", -1)),
                    int(state.get("k_endog", state.get("k", -1))),
                    int(state.get("m_exog", state.get("m", -1))),
                    int(state.get("L_instruments", state.get("L", -1))),
                    str(state.get("covariance", "unknown")),
                    "rejected",
                    error_code,
                    json.dumps(state, default=_json_default),
                    json.dumps([]),
                    0,
                    None,
                ),
            )

    def get_run(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM runs WHERE request_id = ?", (request_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["key_state"] = json.loads(result["key_state"])
            result["warnings"] = json.loads(result["warnings"])
            coefs = con.execute(
                "SELECT name, estimate, std_error, p_value FROM coefficients WHERE request_id = ?",
                (request_id,),
            ).fetchall()
            result["coefficients"] = [dict(c) for c in coefs]
            return result

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT request_id, created_at, status, error_code, nobs FROM runs "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]


def _json_default(obj: Any) -> Any:
    import numpy as np

    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"not JSON serializable: {type(obj)!r}")
