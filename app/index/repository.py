"""SQLite 持久化仓储。

设计约定：

* 一个索引对应一个 SQLite 文件；表结构带版本号，写入在单事务内完成；
* 开启 ``PRAGMA foreign_keys`` 与 ``PRAGMA integrity_check``；
* 所有 SQL 均参数化，不拼接用户输入；
* 写入前与读取后都运行 :mod:`app.index.validator` 的引用完整性校验，
  环、悬空状态、不可达状态、计数不一致一律视为失败。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.core.dawg import Dawg
from app.index.errors import (
    IndexError,
    IndexIntegrityError,
    VIOLATION_META_MISSING,
    IntegrityViolation,
)
from app.index.validator import (
    ROOT_ID,
    StoredEdge,
    StoredState,
    validate_references,
)

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StoredBundle:
    """从存储读出的全部记录。"""

    metadata: dict[str, str]
    states: tuple[StoredState, ...]
    edges: tuple[StoredEdge, ...]


class SQLiteIndexRepository:
    """DAWG 的 SQLite 存储。每次操作用短连接，连接不是长期共享资源。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # ---- 连接 -------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS index_metadata (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS states (
                state_id    INTEGER PRIMARY KEY,
                is_final    INTEGER NOT NULL CHECK (is_final IN (0, 1)),
                word_count  INTEGER NOT NULL CHECK (word_count >= 0)
            );

            CREATE TABLE IF NOT EXISTS edges (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                source    INTEGER NOT NULL REFERENCES states(state_id),
                symbol    TEXT NOT NULL,
                target    INTEGER NOT NULL REFERENCES states(state_id),
                UNIQUE (source, symbol)
            );

            CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
            CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
            """
        )

    # ---- 写入 -------------------------------------------------------------

    def save(
        self,
        dawg: Dawg,
        *,
        index_name: str,
        allow_empty_word: bool,
        source_fixture: str | None = None,
        build_version: str = "unknown",
    ) -> StoredBundle:
        """把 DAWG 原子写入 SQLite；写入前先做引用完整性校验。"""
        states, edges = self._dawg_to_records(dawg)
        self._raise_if_invalid(states, edges)

        metadata = {
            "schema_version": str(SCHEMA_VERSION),
            "index_name": index_name,
            "allow_empty_word": str(bool(allow_empty_word)),
            "source_fixture": source_fixture or "",
            "build_version": build_version,
            "word_count": str(dawg.total_words()),
            "state_count": str(len(states)),
            "edge_count": str(len(edges)),
        }

        conn = self._connect()
        try:
            self._create_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM edges")
            conn.execute("DELETE FROM states")
            conn.execute("DELETE FROM index_metadata")
            conn.executemany(
                "INSERT INTO states (state_id, is_final, word_count) VALUES (?, ?, ?)",
                [(s.state_id, 1 if s.final else 0, s.word_count) for s in states],
            )
            conn.executemany(
                "INSERT INTO edges (source, symbol, target) VALUES (?, ?, ?)",
                [(e.source, e.symbol, e.target) for e in edges],
            )
            conn.executemany(
                "INSERT INTO index_metadata (key, value) VALUES (?, ?)",
                list(metadata.items()),
            )
            # SQLite 自身的结构健全性检查（与本项目引用校验互补）。
            check = conn.execute("PRAGMA integrity_check").fetchone()
            if check is None or check[0] != "ok":
                raise IndexError(f"SQLite integrity_check 未通过: {check}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return StoredBundle(metadata=metadata, states=tuple(states), edges=tuple(edges))

    # ---- 读取 -------------------------------------------------------------

    def load_bundle(self) -> StoredBundle:
        conn = self._connect()
        try:
            self._create_schema(conn)
            meta_rows = conn.execute(
                "SELECT key, value FROM index_metadata"
            ).fetchall()
            metadata = {row["key"]: row["value"] for row in meta_rows}
            if not metadata:
                raise IndexIntegrityError(
                    [
                        IntegrityViolation(
                            kind=VIOLATION_META_MISSING,
                            detail=f"{self.path} 中没有索引元数据，可能尚未构建",
                        )
                    ]
                )
            state_rows = conn.execute(
                "SELECT state_id, is_final, word_count FROM states ORDER BY state_id"
            ).fetchall()
            edge_rows = conn.execute(
                "SELECT source, symbol, target FROM edges "
                "ORDER BY source, id"
            ).fetchall()
        finally:
            conn.close()

        states = [
            StoredState(
                state_id=row["state_id"],
                final=bool(row["is_final"]),
                word_count=row["word_count"],
            )
            for row in state_rows
        ]
        edges = [
            StoredEdge(source=row["source"], symbol=row["symbol"], target=row["target"])
            for row in edge_rows
        ]
        self._raise_if_invalid(states, edges)
        return StoredBundle(
            metadata=metadata, states=tuple(states), edges=tuple(edges)
        )

    def load_dawg(self) -> Dawg:
        """读出记录、校验后重建不可变 :class:`Dawg`。"""
        bundle = self.load_bundle()
        return self._records_to_dawg(bundle)

    # ---- 校验与转换 -------------------------------------------------------

    def _raise_if_invalid(
        self, states: list[StoredState], edges: list[StoredEdge]
    ) -> None:
        violations = validate_references(states, edges, root_id=ROOT_ID)
        if violations:
            raise IndexIntegrityError(violations)

    @staticmethod
    def _dawg_to_records(
        dawg: Dawg,
    ) -> tuple[list[StoredState], list[StoredEdge]]:
        states = [
            StoredState(
                state_id=sid,
                final=dawg.states[sid].final,
                word_count=dawg.word_counts[sid],
            )
            for sid in sorted(dawg.states)
        ]
        edges = [StoredEdge(s, sym, t) for s, sym, t in dawg.iter_edges()]
        return states, edges

    @staticmethod
    def _records_to_dawg(bundle: StoredBundle) -> Dawg:
        from types import MappingProxyType

        from app.core.state import State
        from app.core.dawg import BuildStats

        states: dict[int, State] = {}
        adjacency: dict[int, dict[str, int]] = {}
        counts: dict[int, int] = {}
        for record in bundle.states:
            states[record.state_id] = State(
                state_id=record.state_id,
                final=record.final,
                transitions=MappingProxyType({}),
            )
            counts[record.state_id] = record.word_count or 0
            adjacency[record.state_id] = {}
        for edge in bundle.edges:
            adjacency[edge.source][edge.symbol] = edge.target
        for sid in list(states):
            states[sid] = State(
                state_id=sid,
                final=states[sid].final,
                transitions=MappingProxyType(
                    dict(sorted(adjacency[sid].items()))
                ),
            )

        total = counts.get(ROOT_ID, 0)
        stats = BuildStats(
            accepted_words=total,
            duplicate_words=0,
            raw_states_created=len(states),
            final_state_count=len(states),
            merge_count=0,
            steps=("从持久化存储重建",),
        )
        return Dawg(
            states=states,
            root_id=ROOT_ID,
            word_counts=MappingProxyType(counts),
            stats=stats,
        )

    # ---- 诊断辅助 ---------------------------------------------------------

    def exists(self) -> bool:
        return self.path.exists()

    def raw_tables_for_diagnostics(self) -> dict[str, list[dict]]:
        """只读导出表内容，供损坏注入测试与诊断脚本使用。"""
        conn = self._connect()
        try:
            tables = ("index_metadata", "states", "edges")
            out: dict[str, list[dict]] = {}
            for table in tables:
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                out[table] = [dict(row) for row in rows]
            return out
        finally:
            conn.close()
