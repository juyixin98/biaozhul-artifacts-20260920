"""仓储层：在事务内操作 SQLite。

所有写方法都接收当前事务连接 ``conn``，由服务层用
``Database.write_tx()`` 划定事务边界，保证"读旧分配→占槽→写新分配→
写审计"整条路径原子。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from ..contracts import AllocationContract, CONTRACT_SPEC_VERSION
from ..errors import AppError, ErrorCategory


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class Repository:
    # ---------------- 研究 ----------------
    def insert_study(self, conn: sqlite3.Connection, contract: AllocationContract,
                     fingerprint: str) -> None:
        try:
            conn.execute(
                "INSERT INTO studies(study_id, contract_json, fingerprint, "
                "seed_proof, spec_version, contract_version, created_at, sealed) "
                "VALUES (?,?,?,?,?,?,?,0)",
                (
                    contract.study_id,
                    contract.canonical_payload().decode("utf-8"),
                    fingerprint,
                    contract.seed_proof,
                    CONTRACT_SPEC_VERSION,
                    contract.version,
                    contract.created_at or utc_now_iso(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise AppError(
                ErrorCategory.STUDY_ALREADY_EXISTS, 409,
                f"研究 {contract.study_id!r} 已存在（或契约指纹冲突）",
            ) from exc

    def get_study_row(self, conn: sqlite3.Connection, study_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM studies WHERE study_id=?", (study_id,)
        ).fetchone()
        if row is None:
            raise AppError(
                ErrorCategory.UNKNOWN_STUDY, 404, f"研究 {study_id!r} 不存在"
            )
        return row

    # ---------------- 分层 ----------------
    def ensure_stratum(self, conn: sqlite3.Connection, study_id: str,
                       stratum_key: str) -> int:
        conn.execute(
            "INSERT INTO strata(study_id, stratum_key, status) VALUES (?,?, 'open') "
            "ON CONFLICT(study_id, stratum_key) DO NOTHING",
            (study_id, stratum_key),
        )
        row = conn.execute(
            "SELECT id, status FROM strata WHERE study_id=? AND stratum_key=?",
            (study_id, stratum_key),
        ).fetchone()
        return int(row["id"])

    def seal_stratum(self, conn: sqlite3.Connection, study_id: str,
                     stratum_key: str) -> None:
        conn.execute(
            "UPDATE strata SET status='sealed', sealed_at=? "
            "WHERE study_id=? AND stratum_key=?",
            (utc_now_iso(), study_id, stratum_key),
        )

    def list_strata(self, conn: sqlite3.Connection, study_id: str) -> list[sqlite3.Row]:
        return list(conn.execute(
            "SELECT * FROM strata WHERE study_id=? ORDER BY id", (study_id,)
        ).fetchall())

    # ---------------- 区组 ----------------
    def get_open_block(self, conn: sqlite3.Connection, study_id: str,
                       stratum_key: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM blocks WHERE study_id=? AND stratum_key=? AND status='open' "
            "ORDER BY block_index DESC LIMIT 1",
            (study_id, stratum_key),
        ).fetchone()

    def insert_block(self, conn: sqlite3.Connection, study_id: str,
                     stratum_key: str, block_index: int, block_size: int,
                     slot_arms: list[str],
                     perm: tuple[int, ...] | None = None) -> int:
        try:
            cur = conn.execute(
                "INSERT INTO blocks(study_id, stratum_key, block_index, block_size, "
                "perm_json, slot_arms_json, filled, status) "
                "VALUES (?,?,?,?,?,?,0,'open')",
                (study_id, stratum_key, block_index, block_size,
                 json.dumps(list(perm)) if perm is not None else None,
                 json.dumps(slot_arms)),
            )
        except sqlite3.IntegrityError as exc:  # 并发下同一区组被抢先创建
            raise AppError(
                ErrorCategory.ALLOCATION_RACE, 409,
                "区组创建发生并发冲突，请重试本请求（幂等）",
                {"block_index": block_index},
            ) from exc
        return int(cur.lastrowid)

    def record_block_permutation(self, conn: sqlite3.Connection, block_id: int,
                                 perm: tuple[int, ...]) -> None:
        """区组排满/封闭后把置换留档（此时已无未来次序可泄露）。"""
        conn.execute(
            "UPDATE blocks SET perm_json=? WHERE id=?",
            (json.dumps(list(perm)), block_id),
        )

    def next_block_index(self, conn: sqlite3.Connection, study_id: str,
                         stratum_key: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(block_index)+1, 0) AS next_index "
            "FROM blocks WHERE study_id=? AND stratum_key=?",
            (study_id, stratum_key),
        ).fetchone()
        return int(row["next_index"])

    def claim_next_slot(self, conn: sqlite3.Connection, block_id: int) -> int:
        """在区组内原子占一个入组位置，返回 0 基位置。

        ``filled < block_size`` 条件 + 单写者事务保证不会超发；
        占满后区组翻为 'full'。
        """
        cur = conn.execute(
            "UPDATE blocks SET filled=filled+1 "
            "WHERE id=? AND filled < block_size AND status='open'",
            (block_id,),
        )
        if cur.rowcount != 1:
            raise AppError(
                ErrorCategory.ALLOCATION_RACE, 409,
                "目标区组已满或已封闭，请重试本请求（幂等）",
            )
        row = conn.execute(
            "SELECT filled, block_size FROM blocks WHERE id=?", (block_id,)
        ).fetchone()
        position = int(row["filled"]) - 1
        if int(row["filled"]) == int(row["block_size"]):
            conn.execute(
                "UPDATE blocks SET status='full' WHERE id=?", (block_id,)
            )
        return position

    def list_blocks(self, conn: sqlite3.Connection, study_id: str,
                    stratum_key: str | None = None) -> list[sqlite3.Row]:
        if stratum_key is None:
            return list(conn.execute(
                "SELECT * FROM blocks WHERE study_id=? "
                "ORDER BY stratum_key, block_index", (study_id,)
            ).fetchall())
        return list(conn.execute(
            "SELECT * FROM blocks WHERE study_id=? AND stratum_key=? "
            "ORDER BY block_index", (study_id, stratum_key)
        ).fetchall())

    # ---------------- 分配 ----------------
    def get_allocation_by_subject(self, conn: sqlite3.Connection, study_id: str,
                                  subject_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM allocations WHERE study_id=? AND subject_id=?",
            (study_id, subject_id),
        ).fetchone()

    def get_allocation_by_idem_key(self, conn: sqlite3.Connection, study_id: str,
                                   idempotency_key: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM allocations WHERE study_id=? AND idempotency_key=?",
            (study_id, idempotency_key),
        ).fetchone()

    def insert_allocation(self, conn: sqlite3.Connection, *, study_id: str,
                          subject_id: str, stratum_key: str, block_id: int,
                          position: int, arm: str, features_digest: str,
                          features_canonical: bytes, idempotency_key: str | None,
                          request_id: str, created_at: str) -> None:
        try:
            conn.execute(
                "INSERT INTO allocations(study_id, subject_id, stratum_key, "
                "block_id, position, arm, features_digest, features_canonical, "
                "idempotency_key, request_id, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (study_id, subject_id, stratum_key, block_id, position, arm,
                 features_digest, features_canonical, idempotency_key,
                 request_id, created_at),
            )
        except sqlite3.IntegrityError as exc:
            msg = str(exc)
            if "subject_id" in msg:
                category = ErrorCategory.FEATURES_CHANGED_AFTER_ALLOCATION
                message = "该对象已分配；特征变更不会触发重新随机"
            elif "idempotency_key" in msg:
                category = ErrorCategory.IDEMPOTENCY_KEY_REUSE_CONFLICT
                message = "幂等键已被另一个对象占用"
            else:
                category = ErrorCategory.ALLOCATION_RACE
                message = "槽位并发冲突，请重试（幂等）"
            raise AppError(category, 409, message) from exc

    def list_allocations(self, conn: sqlite3.Connection, study_id: str,
                         limit: int = 100_000) -> list[sqlite3.Row]:
        return list(conn.execute(
            "SELECT * FROM allocations WHERE study_id=? "
            "ORDER BY stratum_key, block_id, position LIMIT ?",
            (study_id, limit),
        ).fetchall())

    # ---------------- 审计 ----------------
    def insert_audit(self, conn: sqlite3.Connection, *, request_id: str,
                     study_id: str | None, subject_id: str | None, actor_role: str,
                     action: str, outcome: str, category: str | None = None,
                     contract_fingerprint: str | None = None,
                     stream_locators: Any = None, detail: dict | None = None,
                     spec_version: str | None = CONTRACT_SPEC_VERSION) -> None:
        conn.execute(
            "INSERT INTO audit_events(ts, request_id, study_id, subject_id, "
            "actor_role, action, outcome, category, contract_fingerprint, "
            "stream_locators_json, detail_json, spec_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (utc_now_iso(), request_id, study_id, subject_id, actor_role, action,
             outcome, category, contract_fingerprint,
             json.dumps(stream_locators, ensure_ascii=False) if stream_locators else None,
             json.dumps(detail or {}, ensure_ascii=False), spec_version),
        )

    def query_audit(self, conn: sqlite3.Connection, study_id: str,
                    limit: int) -> list[sqlite3.Row]:
        return list(conn.execute(
            "SELECT * FROM audit_events WHERE study_id=? ORDER BY id DESC LIMIT ?",
            (study_id, limit),
        ).fetchall())
