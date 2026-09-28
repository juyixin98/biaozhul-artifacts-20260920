"""Repository: typed persistence operations over the SQLite evidence store.

A single connection may be used from FastAPI's thread-pool workers; every
public method takes an instance lock so sqlite3 cursors never interleave.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import FrozenSet, List, Optional, Sequence, Tuple

from ..core.atms import PropagateResult
from ..rules.language import RuleSet
from .schema import transaction


def _env_to_json(env: FrozenSet[str]) -> str:
    # Canonical ordering so equal sets have identical keys.
    return json.dumps(sorted(env), ensure_ascii=False)


def _env_from_json(blob: str) -> FrozenSet[str]:
    return frozenset(json.loads(blob))


class ProblemNotFound(KeyError):
    pass


class Repository:
    """All SQL lives here; the service layer speaks domain objects only."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._lock = threading.RLock()

    # -------------------------------------------------------------- problems

    def save_problem(self, problem_id: str, name: str, source: str,
                     rs: RuleSet) -> None:
        with self._lock, transaction(self.conn):
            self.conn.execute(
                "INSERT INTO problems(id, name, source_dsl) VALUES (?,?,?)",
                (problem_id, name, source),
            )
            self.conn.executemany(
                "INSERT INTO assumptions(problem_id, node_id, ord) VALUES (?,?,?)",
                [(problem_id, a, i) for i, a in enumerate(rs.assumptions)],
            )
            self.conn.executemany(
                "INSERT INTO facts(problem_id, node_id, ord) VALUES (?,?,?)",
                [(problem_id, f, i) for i, f in enumerate(rs.facts)],
            )
            self.conn.executemany(
                "INSERT INTO rules(problem_id, rule_id, antecedents, "
                "consequent, ord) VALUES (?,?,?,?,?)",
                [
                    (problem_id, r.rule_id,
                     json.dumps(list(r.antecedents)), r.consequent, i)
                    for i, r in enumerate(rs.rules)
                ],
            )

    def get_problem_source(self, problem_id: str) -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT source_dsl FROM problems WHERE id = ?", (problem_id,)
            ).fetchone()
            if row is None:
                raise ProblemNotFound(problem_id)
            return row["source_dsl"]

    def list_problems(self) -> List[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, name, created_at FROM problems ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def problem_exists(self, problem_id: str) -> bool:
        with self._lock:
            return self.conn.execute(
                "SELECT 1 FROM problems WHERE id = ?", (problem_id,)
            ).fetchone() is not None

    # ------------------------------------------------------------- snapshots

    def replace_snapshot(
        self,
        problem_id: str,
        labels: dict,
        nogoods: Sequence[FrozenSet[str]],
    ) -> None:
        """Persist the current label/nogood state, replacing any prior one."""
        with self._lock, transaction(self.conn):
            self.conn.execute(
                "DELETE FROM label_snapshots WHERE problem_id = ?", (problem_id,)
            )
            self.conn.execute(
                "DELETE FROM nogood_snapshots WHERE problem_id = ?", (problem_id,)
            )
            rows = []
            for node_id in sorted(labels):
                for ord_, env in enumerate(
                    sorted(labels[node_id], key=lambda e: (len(e), sorted(e)))
                ):
                    rows.append((problem_id, node_id, _env_to_json(env), ord_))
            self.conn.executemany(
                "INSERT INTO label_snapshots(problem_id, node_id, env_json, ord) "
                "VALUES (?,?,?,?)",
                rows,
            )
            self.conn.executemany(
                "INSERT INTO nogood_snapshots(problem_id, env_json, ord) "
                "VALUES (?,?,?)",
                [
                    (problem_id, _env_to_json(env), i)
                    for i, env in enumerate(
                        sorted(nogoods, key=lambda e: (len(e), sorted(e)))
                    )
                ],
            )

    def load_snapshot(
        self, problem_id: str
    ) -> Optional[Tuple[dict, List[FrozenSet[str]]]]:
        """Return (labels, nogoods) or None when no snapshot exists yet."""
        with self._lock:
            if not self.problem_exists_unlocked(problem_id):
                raise ProblemNotFound(problem_id)
            lrows = self.conn.execute(
                "SELECT node_id, env_json FROM label_snapshots "
                "WHERE problem_id = ? ORDER BY node_id, ord",
                (problem_id,),
            ).fetchall()
            nrows = self.conn.execute(
                "SELECT env_json FROM nogood_snapshots "
                "WHERE problem_id = ? ORDER BY ord",
                (problem_id,),
            ).fetchall()
            if not lrows and not nrows:
                # Facts always produce empty-environment labels, so a fully
                # propagated problem has at least label rows; no rows = no run.
                return None
            labels: dict = {}
            for r in lrows:
                labels.setdefault(r["node_id"], []).append(
                    _env_from_json(r["env_json"])
                )
            nogoods = [_env_from_json(r["env_json"]) for r in nrows]
            return labels, nogoods

    def problem_exists_unlocked(self, problem_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM problems WHERE id = ?", (problem_id,)
        ).fetchone() is not None

    # ------------------------------------------------------------------- runs

    def record_run(self, problem_id: str, request_id: str,
                   result: PropagateResult) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO propagation_runs(problem_id, request_id, "
                "incomplete, reason, steps, total_envs, new_nogoods) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    problem_id,
                    request_id,
                    1 if result.incomplete else 0,
                    result.reason,
                    result.steps,
                    result.total_envs,
                    result.new_nogoods,
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def list_runs(self, problem_id: str, limit: int = 20) -> List[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, request_id, incomplete, reason, steps, total_envs, "
                "new_nogoods, created_at FROM propagation_runs "
                "WHERE problem_id = ? ORDER BY id DESC LIMIT ?",
                (problem_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
