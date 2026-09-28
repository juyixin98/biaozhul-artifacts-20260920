"""索引层：SQLite 持久化（语料、挖掘结果、模型元数据）。

仓储只做存取，不做 FST 计算；编译产物保存在内存模型注册表
（:mod:`wfst.index.registry`），数据库保存可重建的源数据与代价。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from wfst.corpus.mining import MiningReport
from wfst.corpus.spec import CorpusSpec

_SCHEMA = """
CREATE TABLE IF NOT EXISTS corpora (
    corpus_id    TEXT NOT NULL,
    version      TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    token_level  TEXT NOT NULL,
    description  TEXT NOT NULL,
    raw_json     TEXT NOT NULL,
    loaded_at    TEXT NOT NULL,
    PRIMARY KEY (corpus_id, version)
);
CREATE TABLE IF NOT EXISTS lexicon_entries (
    corpus_id TEXT NOT NULL,
    version   TEXT NOT NULL,
    text_in   TEXT NOT NULL,
    text_out  TEXT NOT NULL,
    cost      REAL NOT NULL,
    cnt       INTEGER NOT NULL,
    PRIMARY KEY (corpus_id, version, text_in, text_out)
);
CREATE TABLE IF NOT EXISTS mined_arcs (
    corpus_id TEXT NOT NULL,
    version   TEXT NOT NULL,
    ilabel    TEXT NOT NULL,
    olabel    TEXT NOT NULL,
    op        TEXT NOT NULL,
    cost      REAL NOT NULL,
    cnt       INTEGER NOT NULL,
    PRIMARY KEY (corpus_id, version, ilabel, olabel, op)
);
"""


class Store:
    """SQLite 仓储（单文件，本地依赖，启动时建表）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def upsert_corpus(
        self, spec: CorpusSpec, raw_bytes: bytes, fingerprint: str
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO corpora(corpus_id, version, fingerprint, token_level,
                                description, raw_json, loaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(corpus_id, version) DO UPDATE SET
                fingerprint=excluded.fingerprint,
                token_level=excluded.token_level,
                description=excluded.description,
                raw_json=excluded.raw_json,
                loaded_at=excluded.loaded_at
            """,
            (
                spec.corpus_id,
                spec.version,
                fingerprint,
                spec.token_level,
                spec.description,
                raw_bytes.decode("utf-8"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.execute(
            "DELETE FROM lexicon_entries WHERE corpus_id=? AND version=?",
            (spec.corpus_id, spec.version),
        )
        self._conn.execute(
            "DELETE FROM mined_arcs WHERE corpus_id=? AND version=?",
            (spec.corpus_id, spec.version),
        )
        self._conn.commit()

    def save_mining(self, report: MiningReport) -> None:
        rows = [
            (
                report.corpus_id,
                report.version,
                e.text_in,
                e.text_out,
                e.cost,
                e.count,
            )
            for e in report.lexicon_entries
        ]
        self._conn.executemany(
            "INSERT INTO lexicon_entries VALUES (?, ?, ?, ?, ?, ?)", rows
        )
        arcs = [
            (
                report.corpus_id,
                report.version,
                a.ilabel,
                a.olabel,
                a.op.value,
                a.cost,
                a.count,
            )
            for a in report.arcs
        ]
        self._conn.executemany(
            "INSERT INTO mined_arcs VALUES (?, ?, ?, ?, ?, ?, ?)", arcs
        )
        self._conn.commit()

    def list_corpora(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT corpus_id, version, fingerprint, token_level, description, "
            "loaded_at FROM corpora ORDER BY corpus_id, version"
        )
        return [dict(r) for r in cur.fetchall()]

    def get_corpus_raw(self, corpus_id: str, version: str | None = None) -> dict:
        if version is None:
            row = self._conn.execute(
                "SELECT raw_json FROM corpora WHERE corpus_id=? "
                "ORDER BY version DESC LIMIT 1",
                (corpus_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT raw_json FROM corpora WHERE corpus_id=? AND version=?",
                (corpus_id, version),
            ).fetchone()
        if row is None:
            raise KeyError(f"语料 {corpus_id}@{version or 'latest'} 未入库")
        return json.loads(row["raw_json"])

    def get_mined_arcs(self, corpus_id: str, version: str | None = None) -> list[dict]:
        if version is None:
            row = self._conn.execute(
                "SELECT version FROM corpora WHERE corpus_id=? "
                "ORDER BY version DESC LIMIT 1",
                (corpus_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"语料 {corpus_id} 未入库")
            version = row["version"]
        cur = self._conn.execute(
            "SELECT ilabel, olabel, op, cost, cnt FROM mined_arcs "
            "WHERE corpus_id=? AND version=? ORDER BY op, ilabel, olabel",
            (corpus_id, version),
        )
        return [dict(r) for r in cur.fetchall()]
