"""SQLite-backed storage.

Responsibilities
----------------
* Freeze each study's :class:`~stratblock.contract.StudyConfig` (and the
  master seed in force at registration) so it can never mutate underneath
  already-allocated subjects.
* Serialise concurrent enrollments (thread lock + transaction) so that a
  subject allocates exactly once even under simultaneous requests.
* Return the prior allocation for repeat requests (idempotency), and
  distinguish a benign repeat from a *feature-changing* repeat.
* Append-only audit events, recorded before callers can observe success.

No allocation randomness is generated here — this layer only persists the
pure kernel state from :mod:`stratblock.rng`.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contract import (
    STREAM_SCHEME,
    AllocationError,
    ErrorCategory,
    StudyConfig,
)
from .rng import StratumRuntimeState, initial_state


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class EnrollmentResult:
    study_id: str
    subject_id: str
    arm: str
    stratum_key: str
    features: dict[str, str]
    block_index: int
    position_in_block: int
    block_size: int
    sequence_index: int
    allocated_at: str
    request_id: str
    replayed: bool
    state: StratumRuntimeState


SCHEMA = """
CREATE TABLE IF NOT EXISTS studies (
    study_id      TEXT PRIMARY KEY,
    config_json   TEXT NOT NULL,
    master_seed   INTEGER NOT NULL,
    stream_scheme TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strata (
    study_id    TEXT NOT NULL,
    stratum_key TEXT NOT NULL,
    state_json  TEXT NOT NULL,
    PRIMARY KEY (study_id, stratum_key)
);
CREATE TABLE IF NOT EXISTS subjects (
    study_id          TEXT NOT NULL,
    subject_id        TEXT NOT NULL,
    features_json     TEXT NOT NULL,
    stratum_key       TEXT NOT NULL,
    arm               TEXT NOT NULL,
    block_index       INTEGER NOT NULL,
    position_in_block INTEGER NOT NULL,
    block_size        INTEGER NOT NULL,
    sequence_index    INTEGER NOT NULL,
    request_id        TEXT NOT NULL,
    allocated_at      TEXT NOT NULL,
    PRIMARY KEY (study_id, subject_id)
);
CREATE TABLE IF NOT EXISTS idempotency (
    request_id TEXT PRIMARY KEY,
    study_id   TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id   TEXT NOT NULL,
    subject_id TEXT,
    request_id TEXT,
    event_type TEXT NOT NULL,
    actor      TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (
    study_id   TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    y          REAL NOT NULL,
    PRIMARY KEY (study_id, subject_id)
);
"""


class Storage:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            isolation_level="",  # explicit transaction control
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- studies

    def register_study(self, cfg: StudyConfig, master_seed: int) -> None:
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT 1 FROM studies WHERE study_id=?", (cfg.study_id,)
            ).fetchone()
            if existing is not None:
                raise AllocationError(
                    ErrorCategory.CONFLICT,
                    f"study {cfg.study_id!r} is already registered; "
                    "the frozen contract cannot be modified",
                    {"study_id": cfg.study_id},
                    http_status=409,
                )
            now = _utcnow()
            self._conn.execute(
                "INSERT INTO studies(study_id, config_json, master_seed, "
                "stream_scheme, status, created_at) VALUES (?,?,?,?,?,?)",
                (
                    cfg.study_id,
                    cfg.to_json(),
                    master_seed,
                    STREAM_SCHEME,
                    "active",
                    now,
                ),
            )
            self._append_event(
                cfg.study_id, None, None, "STUDY_REGISTERED", "administrator",
                {
                    "config": json.loads(cfg.to_json()),
                    "master_seed": master_seed,
                    "stream_scheme": STREAM_SCHEME,
                },
                at=now,
            )

    def get_study(self, study_id: str) -> tuple[StudyConfig, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT config_json, master_seed FROM studies WHERE study_id=?",
                (study_id,),
            ).fetchone()
        if row is None:
            raise AllocationError(
                ErrorCategory.UNKNOWN_STUDY,
                f"unknown study {study_id!r}",
                {"study_id": study_id},
                http_status=404,
            )
        return StudyConfig.from_json(row["config_json"]), int(row["master_seed"])

    def list_studies(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT study_id, config_json, master_seed, status, created_at "
                "FROM studies ORDER BY study_id"
            ).fetchall()
        return [
            {
                "study_id": r["study_id"],
                "config": json.loads(r["config_json"]),
                "master_seed": int(r["master_seed"]),
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------- enrollment

    def _load_stratum_locked(self, study_id: str, key: str) -> StratumRuntimeState:
        row = self._conn.execute(
            "SELECT state_json FROM strata WHERE study_id=? AND stratum_key=?",
            (study_id, key),
        ).fetchone()
        if row is None:
            return initial_state()
        return StratumRuntimeState.from_dict(json.loads(row["state_json"]))

    def _save_stratum_locked(
        self, study_id: str, key: str, state: StratumRuntimeState
    ) -> None:
        self._conn.execute(
            "INSERT INTO strata(study_id, stratum_key, state_json) VALUES (?,?,?) "
            "ON CONFLICT(study_id, stratum_key) DO UPDATE SET state_json=excluded.state_json",
            (study_id, key, json.dumps(state.to_dict(), sort_keys=True)),
        )

    def _append_event(
        self,
        study_id: str,
        subject_id: str | None,
        request_id: str | None,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        at: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO audit_events(study_id, subject_id, request_id, event_type, "
            "actor, payload_json, at) VALUES (?,?,?,?,?,?,?)",
            (
                study_id,
                subject_id,
                request_id,
                event_type,
                actor,
                json.dumps(payload, sort_keys=True),
                at or _utcnow(),
            ),
        )
        return int(cur.lastrowid or 0)

    def lookup_subject(
        self, study_id: str, subject_id: str
    ) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM subjects WHERE study_id=? AND subject_id=?",
                (study_id, subject_id),
            ).fetchone()

    def enroll(
        self,
        cfg: StudyConfig,
        master_seed: int,
        subject_id: str,
        features: dict[str, str],
        request_id: str,
        actor: str,
        resolve_draw,
    ) -> EnrollmentResult:
        """Idempotent enrollment.

        ``resolve_draw(cfg, stream_key, state) -> (BlockDraw, new_state)`` is
        injected so storage stays independent of the RNG module's stream
        derivation. The whole decision runs inside one lock+transaction.
        """
        features_json = json.dumps(features, sort_keys=True)
        with self._lock, self._conn:
            # Repeat requests resolve (return / refuse) even when the
            # study is sealed: reading an existing allocation consumes no
            # randomness and must not depend on enrollment being open.
            row = self._conn.execute(
                "SELECT * FROM subjects WHERE study_id=? AND subject_id=?",
                (cfg.study_id, subject_id),
            ).fetchone()
            if row is not None:
                return self._handle_repeat(
                    cfg, row, features, features_json, request_id, actor
                )

            status_row = self._conn.execute(
                "SELECT status FROM studies WHERE study_id=?", (cfg.study_id,)
            ).fetchone()
            if status_row is not None and status_row["status"] != "active":
                raise AllocationError(
                    ErrorCategory.STRATUM_CLOSED,
                    f"study {cfg.study_id!r} is {status_row['status']}; "
                    "enrollment is closed",
                    {"study_id": cfg.study_id, "status": status_row["status"]},
                    http_status=409,
                )

            idem = self._conn.execute(
                "SELECT study_id, subject_id, outcome FROM idempotency WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if idem is not None and (
                idem["study_id"] != cfg.study_id or idem["subject_id"] != subject_id
            ):
                raise AllocationError(
                    ErrorCategory.CONFLICT,
                    "request_id was already used for a different subject",
                    {
                        "request_id": request_id,
                        "existing_study_id": idem["study_id"],
                        "existing_subject_id": idem["subject_id"],
                    },
                    http_status=409,
                )

            from .contract import canonical_stratum_key

            key = canonical_stratum_key(cfg.stratification_factors, features)
            state = self._load_stratum_locked(cfg.study_id, key)
            if state.sealed:
                raise AllocationError(
                    ErrorCategory.STRATUM_CLOSED,
                    f"stratum {key!r} has been sealed; its incomplete tail is closed",
                    {"study_id": cfg.study_id, "stratum_key": key},
                    http_status=409,
                )

            from .rng import derive_stream_key

            stream_key = derive_stream_key(master_seed, cfg.study_id, key)
            draw, new_state = resolve_draw(cfg, stream_key, state)
            self._save_stratum_locked(cfg.study_id, key, new_state)
            now = _utcnow()
            arm = cfg.arms[draw.arm_index]
            self._conn.execute(
                "INSERT INTO subjects(study_id, subject_id, features_json, "
                "stratum_key, arm, block_index, position_in_block, block_size, "
                "sequence_index, request_id, allocated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cfg.study_id,
                    subject_id,
                    features_json,
                    key,
                    arm,
                    draw.block_index,
                    draw.position_in_block,
                    draw.block_size,
                    draw.sequence_index,
                    request_id,
                    now,
                ),
            )
            self._conn.execute(
                "INSERT INTO idempotency(request_id, study_id, subject_id, "
                "outcome, created_at) VALUES (?,?,?,?,?)",
                (request_id, cfg.study_id, subject_id, "ENROLLED", now),
            )
            self._append_event(
                cfg.study_id,
                subject_id,
                request_id,
                "SUBJECT_ENROLLED",
                actor,
                {
                    "arm": arm,
                    "stratum_key": key,
                    "features": features,
                    "block_index": draw.block_index,
                    "position_in_block": draw.position_in_block,
                    "block_size": draw.block_size,
                    "sequence_index": draw.sequence_index,
                    "stream_scheme": STREAM_SCHEME,
                    "rng_detail": draw.rng_detail,
                    "idempotent_replay": False,
                },
                at=now,
            )
            return EnrollmentResult(
                study_id=cfg.study_id,
                subject_id=subject_id,
                arm=arm,
                stratum_key=key,
                features=features,
                block_index=draw.block_index,
                position_in_block=draw.position_in_block,
                block_size=draw.block_size,
                sequence_index=draw.sequence_index,
                allocated_at=now,
                request_id=request_id,
                replayed=False,
                state=new_state,
            )

    def _handle_repeat(
        self,
        cfg: StudyConfig,
        row: sqlite3.Row,
        features: dict[str, str],
        features_json: str,
        request_id: str,
        actor: str,
    ) -> EnrollmentResult:
        prior_features = json.loads(row["features_json"])
        if prior_features != features:
            payload = {
                "subject_id": row["subject_id"],
                "original_features": prior_features,
                "conflicting_features": features,
                "original_stratum_key": row["stratum_key"],
                "original_arm": row["arm"],
                "policy": (
                    "original allocation is retained; features must never "
                    "silently move a subject to another stratum/stream"
                ),
            }
            self._append_event(
                cfg.study_id,
                row["subject_id"],
                request_id,
                "REALLOCATION_REFUSED",
                actor,
                payload,
            )
            # Persist the refusal before raising: otherwise the enclosing
            # transaction context would roll this audit event back along
            # with the exception. ROLLBACK at context exit is then a no-op.
            self._conn.execute("COMMIT")
            raise AllocationError(
                ErrorCategory.DUPLICATE_CONFLICT,
                f"subject {row['subject_id']!r} is already allocated and the "
                "submitted features differ from the original enrollment",
                payload,
                http_status=409,
            )
        state = self._load_stratum_locked(cfg.study_id, row["stratum_key"])
        self._append_event(
            cfg.study_id,
            row["subject_id"],
            request_id,
            "ALLOCATION_RETURNED",
            actor,
            {
                "arm": row["arm"],
                "stratum_key": row["stratum_key"],
                "request_id": request_id,
                "original_request_id": row["request_id"],
                "idempotent_replay": True,
                "note": "same subject + same features returns the original arm; "
                "no randomness was consumed",
            },
        )
        return EnrollmentResult(
            study_id=cfg.study_id,
            subject_id=row["subject_id"],
            arm=row["arm"],
            stratum_key=row["stratum_key"],
            features=prior_features,
            block_index=row["block_index"],
            position_in_block=row["position_in_block"],
            block_size=row["block_size"],
            sequence_index=row["sequence_index"],
            allocated_at=row["allocated_at"],
            request_id=row["request_id"],
            replayed=True,
            state=state,
        )

    # ---------------------------------------------------------------- sealing

    def seal_study(self, study_id: str) -> None:
        """Close enrollment for every stratum of a study."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT status FROM studies WHERE study_id=?", (study_id,)
            ).fetchone()
            if row is None:
                raise AllocationError(
                    ErrorCategory.UNKNOWN_STUDY,
                    f"unknown study {study_id!r}",
                    {"study_id": study_id},
                    http_status=404,
                )
            self._conn.execute(
                "UPDATE studies SET status='sealed' WHERE study_id=?", (study_id,)
            )
            # Flip every stratum's persisted state as well, so the state
            # read back after restart truthfully says "closed" at both
            # levels (study status and per-stratum stream state).
            stratum_rows = self._conn.execute(
                "SELECT stratum_key, state_json FROM strata WHERE study_id=?",
                (study_id,),
            ).fetchall()
            for srow in stratum_rows:
                state_data = json.loads(srow["state_json"])
                state_data["sealed"] = True
                self._conn.execute(
                    "UPDATE strata SET state_json=? WHERE study_id=? AND stratum_key=?",
                    (json.dumps(state_data, sort_keys=True), study_id, srow["stratum_key"]),
                )
                self._append_event(
                    study_id, None, None, "STRATUM_SEALED", "administrator",
                    {"stratum_key": srow["stratum_key"]},
                )
            self._append_event(
                study_id, None, None, "STUDY_SEALED", "administrator",
                {"status": "sealed", "strata_sealed": len(stratum_rows)},
            )

    def list_strata(self, study_id: str) -> list[tuple[str, StratumRuntimeState]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT stratum_key, state_json FROM strata "
                "WHERE study_id=? ORDER BY stratum_key",
                (study_id,),
            ).fetchall()
        return [(r["stratum_key"], StratumRuntimeState.from_dict(json.loads(r["state_json"]))) for r in rows]

    # -------------------------------------------------------------- auditing

    def audit_events(self, study_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, study_id, subject_id, request_id, event_type, actor, "
                "payload_json, at FROM audit_events WHERE study_id=? ORDER BY id",
                (study_id,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "study_id": r["study_id"],
                "subject_id": r["subject_id"],
                "request_id": r["request_id"],
                "event_type": r["event_type"],
                "actor": r["actor"],
                "payload": json.loads(r["payload_json"]),
                "at": r["at"],
            }
            for r in rows
        ]

    def subjects(self, study_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT subject_id, features_json, stratum_key, arm, block_index, "
                "position_in_block, block_size, sequence_index, request_id, allocated_at "
                "FROM subjects WHERE study_id=? ORDER BY sequence_index, stratum_key, subject_id",
                (study_id,),
            ).fetchall()
        return [
            {
                "subject_id": r["subject_id"],
                "features": json.loads(r["features_json"]),
                "stratum_key": r["stratum_key"],
                "arm": r["arm"],
                "block_index": r["block_index"],
                "position_in_block": r["position_in_block"],
                "block_size": r["block_size"],
                "sequence_index": r["sequence_index"],
                "request_id": r["request_id"],
                "allocated_at": r["allocated_at"],
            }
            for r in rows
        ]

    # ---------------------------------------------------------------- outcomes

    def stratum_block_counts(
        self, study_id: str, stratum_key: str, arms: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """Realised per-arm counts for every block of one stratum."""
        arm_index = {a: i for i, a in enumerate(arms)}
        with self._lock:
            rows = self._conn.execute(
                "SELECT block_index, block_size, position_in_block, arm "
                "FROM subjects WHERE study_id=? AND stratum_key=? "
                "ORDER BY block_index, position_in_block",
                (study_id, stratum_key),
            ).fetchall()
        blocks: dict[int, dict[str, Any]] = {}
        for r in rows:
            b = blocks.setdefault(
                r["block_index"],
                {
                    "block_index": r["block_index"],
                    "block_size": r["block_size"],
                    "counts": [0] * len(arms),
                    "filled": 0,
                },
            )
            b["counts"][arm_index[r["arm"]]] += 1
            b["filled"] += 1
        return [blocks[k] for k in sorted(blocks)]

    def record_outcome(self, study_id: str, subject_id: str, y: float) -> None:
        with self._lock, self._conn:
            subject = self._conn.execute(
                "SELECT 1 FROM subjects WHERE study_id=? AND subject_id=?",
                (study_id, subject_id),
            ).fetchone()
            if subject is None:
                raise AllocationError(
                    ErrorCategory.UNKNOWN_SUBJECT,
                    f"subject {subject_id!r} is not enrolled in study {study_id!r}",
                    {"study_id": study_id, "subject_id": subject_id},
                    http_status=404,
                )
            self._conn.execute(
                "INSERT INTO outcomes(study_id, subject_id, y) VALUES (?,?,?) "
                "ON CONFLICT(study_id, subject_id) DO UPDATE SET y=excluded.y",
                (study_id, subject_id, float(y)),
            )

    def outcomes(self, study_id: str) -> dict[str, list[tuple[str, float]]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.arm, o.subject_id, o.y FROM outcomes o "
                "JOIN subjects s ON s.study_id=o.study_id AND s.subject_id=o.subject_id "
                "WHERE o.study_id=? ORDER BY o.subject_id",
                (study_id,),
            ).fetchall()
        grouped: dict[str, list[tuple[str, float]]] = {}
        for r in rows:
            grouped.setdefault(r["arm"], []).append((r["subject_id"], float(r["y"])))
        return grouped
