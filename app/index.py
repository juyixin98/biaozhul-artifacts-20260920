"""索引与模型：SQLite 持久化、版本管理、重建。

排序键与原文同时落库；meta 表记录构建时的索引版本与规则。
规则升级后 stored_version != kernel.index_version，查询层拒绝服务，
必须显式 rebuild —— 旧游标因此自然失效（游标内嵌版本号）。
"""
from __future__ import annotations

import sqlite3
import unicodedata
from pathlib import Path

from .corpus import CorpusEntry
from .errors import index_not_built, index_version_conflict
from .kernel import CollationKernel

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    id       TEXT PRIMARY KEY,
    seq      INTEGER NOT NULL,
    original TEXT NOT NULL,
    nfc      TEXT NOT NULL,
    sort_key BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_sort ON entries(sort_key, seq);
"""


class SQLiteIndex:
    """排序键索引。BLOB 列按 memcmp 比较，与 ICU 排序键字节序一致。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        # FastAPI 在线程池中运行端点；单进程服务下关闭同线程限制，
        # 连接本身的并发由 SQLite 序列化模式保证。
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- 版本管理 ----

    def stored_version(self) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'index_version'"
        ).fetchone()
        return row["value"] if row else None

    def stored_rules_json(self) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'rules_json'"
        ).fetchone()
        return row["value"] if row else None

    def require_version(self, kernel: CollationKernel) -> str:
        """校验索引与内核版本一致，否则抛出对应失败类别。"""
        stored = self.stored_version()
        if stored is None:
            raise index_not_built(
                "索引尚未构建，请先调用 /admin/rebuild",
                kernel_version=kernel.index_version,
            )
        if stored != kernel.index_version:
            raise index_version_conflict(
                "排序规则或 ICU 版本已变化，索引需重建后才能服务",
                stored_version=stored,
                kernel_version=kernel.index_version,
            )
        return stored

    # ---- 构建 ----

    def rebuild(self, kernel: CollationKernel, entries: list[CorpusEntry]) -> int:
        """全量重建：按内核排序键重写 entries 表并更新版本元数据。"""
        keyed = kernel.sort_entries(entries)
        with self._conn:
            self._conn.execute("DELETE FROM entries")
            self._conn.executemany(
                "INSERT INTO entries(id, seq, original, nfc, sort_key)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        entry.id,
                        entry.seq,
                        entry.text,
                        unicodedata.normalize("NFC", entry.text),
                        key,
                    )
                    for entry, key in keyed
                ],
            )
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('index_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (kernel.index_version,),
            )
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('rules_json', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (kernel.rules.to_json(),),
            )
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('icu_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (kernel.icu_version,),
            )
        return len(keyed)

    # ---- 读取 ----

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()
        return row["n"]

    def get(self, entry_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT id, seq, original, nfc, sort_key FROM entries WHERE id = ?",
            (entry_id,),
        ).fetchone()

    def fetch_page(
        self, after: tuple[bytes, int] | None, limit: int
    ) -> list[sqlite3.Row]:
        """按 (sort_key, seq) 全序取一页；after 为上一页最后一行的位置。"""
        if after is None:
            return self._conn.execute(
                "SELECT id, seq, original, nfc, sort_key FROM entries"
                " ORDER BY sort_key, seq LIMIT ?",
                (limit,),
            ).fetchall()
        return self._conn.execute(
            "SELECT id, seq, original, nfc, sort_key FROM entries"
            " WHERE (sort_key, seq) > (?, ?) ORDER BY sort_key, seq LIMIT ?",
            (after[0], after[1], limit),
        ).fetchall()

    def fetch_range(
        self,
        lower_key: bytes,
        upper_key: bytes,
        *,
        lower_inclusive: bool,
        upper_inclusive: bool,
        limit: int,
    ) -> list[sqlite3.Row]:
        """范围检索：边界一律是排序键，绝不使用 UTF-8 字节比较。"""
        lo_op = ">=" if lower_inclusive else ">"
        hi_op = "<=" if upper_inclusive else "<"
        return self._conn.execute(
            f"SELECT id, seq, original, nfc, sort_key FROM entries"
            f" WHERE sort_key {lo_op} ? AND sort_key {hi_op} ?"
            f" ORDER BY sort_key, seq LIMIT ?",
            (lower_key, upper_key, limit),
        ).fetchall()
