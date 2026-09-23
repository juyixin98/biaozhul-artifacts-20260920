"""SQLite 持久化层。

投票一旦通过校验即为只追加（append-only）；双投的两条投票都会落库作为冲突证据。
checkpoint / conflict 为派生态的持久化记录，重启时由全部投票重放重建并与之核对。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS epochs (
    epoch        INTEGER PRIMARY KEY,
    total_weight INTEGER NOT NULL,
    state        TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS validators (
    epoch      INTEGER NOT NULL,
    validator  TEXT NOT NULL,
    weight     INTEGER NOT NULL,
    public_key TEXT NOT NULL,
    PRIMARY KEY (epoch, validator),
    FOREIGN KEY (epoch) REFERENCES epochs(epoch)
);

CREATE TABLE IF NOT EXISTS votes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    epoch        INTEGER NOT NULL,
    validator    TEXT NOT NULL,
    block_hash   TEXT NOT NULL,
    signature    TEXT NOT NULL,
    received_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (epoch) REFERENCES epochs(epoch)
);
CREATE INDEX IF NOT EXISTS idx_votes_epoch ON votes(epoch);

-- 双投（equivocation）证据：每个验证者一行，块与 vote id 以 JSON 数组保存
CREATE TABLE IF NOT EXISTS equivocations (
    epoch       INTEGER NOT NULL,
    validator   TEXT NOT NULL,
    weight      INTEGER NOT NULL,
    block_hashes TEXT NOT NULL,
    vote_ids    TEXT NOT NULL,
    PRIMARY KEY (epoch, validator),
    FOREIGN KEY (epoch) REFERENCES epochs(epoch)
);

-- 终局检查点：每 epoch 至多一行，首次达成即固化
CREATE TABLE IF NOT EXISTS checkpoints (
    epoch           INTEGER PRIMARY KEY,
    block_hash      TEXT NOT NULL,
    weight          INTEGER NOT NULL,
    required_weight INTEGER NOT NULL,
    total_weight    INTEGER NOT NULL,
    finalized_at    TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (epoch) REFERENCES epochs(epoch)
);

-- 终局冲突（安全告警）：每 epoch 至多一行，冻结推进
CREATE TABLE IF NOT EXISTS conflicts (
    epoch     INTEGER PRIMARY KEY,
    evidence  TEXT NOT NULL,
    raised_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (epoch) REFERENCES epochs(epoch)
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库 schema 版本不匹配：文件为 {row['value']}，代码为 {SCHEMA_VERSION}"
                )

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE 串行化写事务，杜绝并发交叉导致的重放缝隙。"""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    # ---- epochs / validators -------------------------------------------

    def epoch_exists(self, conn: sqlite3.Connection, epoch: int) -> bool:
        return conn.execute(
            "SELECT 1 FROM epochs WHERE epoch=?", (epoch,)
        ).fetchone() is not None

    def insert_epoch(
        self,
        conn: sqlite3.Connection,
        epoch: int,
        total_weight: int,
        validators: list[dict[str, Any]],
    ) -> None:
        conn.execute(
            "INSERT INTO epochs(epoch, total_weight) VALUES(?, ?)",
            (epoch, total_weight),
        )
        conn.executemany(
            "INSERT INTO validators(epoch, validator, weight, public_key) VALUES(?,?,?,?)",
            [(epoch, v["id"], int(v["weight"]), v["public_key"]) for v in validators],
        )

    def get_validators(self, epoch: int) -> dict[str, sqlite3.Row]:
        rows = self._conn.execute(
            "SELECT validator, weight, public_key FROM validators WHERE epoch=?",
            (epoch,),
        ).fetchall()
        return {r["validator"]: r for r in rows}

    def get_epoch(self, epoch: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM epochs WHERE epoch=?", (epoch,)
        ).fetchone()

    def list_epochs(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT e.epoch, e.state, e.total_weight, c.block_hash AS finalized_block "
            "FROM epochs e LEFT JOIN checkpoints c ON c.epoch = e.epoch ORDER BY e.epoch"
        ).fetchall()

    # ---- votes -----------------------------------------------------------

    def insert_vote(
        self,
        conn: sqlite3.Connection,
        epoch: int,
        validator: str,
        block_hash: str,
        signature: str,
    ) -> int:
        cur = conn.execute(
            "INSERT INTO votes(epoch, validator, block_hash, signature) VALUES(?,?,?,?)",
            (epoch, validator, block_hash, signature),
        )
        return int(cur.lastrowid)

    def get_votes(self, epoch: int) -> list[sqlite3.Row]:
        """按到达顺序（id 即插入顺序）返回某 epoch 的全部投票。"""
        return self._conn.execute(
            "SELECT id, epoch, validator, block_hash, signature FROM votes "
            "WHERE epoch=? ORDER BY id",
            (epoch,),
        ).fetchall()

    # ---- derived state writes --------------------------------------------

    def replace_equivocations(
        self, conn: sqlite3.Connection, epoch: int, equivs: list[dict[str, Any]]
    ) -> None:
        conn.execute("DELETE FROM equivocations WHERE epoch=?", (epoch,))
        conn.executemany(
            "INSERT INTO equivocations(epoch, validator, weight, block_hashes, vote_ids)"
            " VALUES(?,?,?,?,?)",
            [
                (
                    epoch,
                    e["validator_id"],
                    int(e["weight"]),
                    json.dumps(e["block_hashes"], separators=(",", ":")),
                    json.dumps(e["vote_ids"], separators=(",", ":")),
                )
                for e in equivs
            ],
        )

    def upsert_checkpoint(
        self, conn: sqlite3.Connection, epoch: int, cp: dict[str, Any]
    ) -> None:
        conn.execute(
            "INSERT INTO checkpoints(epoch, block_hash, weight, required_weight, total_weight)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(epoch) DO UPDATE SET"
            " block_hash=excluded.block_hash, weight=excluded.weight,"
            " required_weight=excluded.required_weight, total_weight=excluded.total_weight",
            (
                epoch,
                cp["block_hash"],
                int(cp["weight"]),
                int(cp["required_weight"]),
                int(cp["total_weight"]),
            ),
        )

    def delete_checkpoint(self, conn: sqlite3.Connection, epoch: int) -> None:
        conn.execute("DELETE FROM checkpoints WHERE epoch=?", (epoch,))

    def upsert_conflict(
        self, conn: sqlite3.Connection, epoch: int, evidence: dict[str, Any]
    ) -> None:
        conn.execute(
            "INSERT INTO conflicts(epoch, evidence) VALUES(?,?)"
            " ON CONFLICT(epoch) DO UPDATE SET evidence=excluded.evidence",
            (epoch, json.dumps(evidence, separators=(",", ":"), sort_keys=True)),
        )

    def set_epoch_state(
        self, conn: sqlite3.Connection, epoch: int, state: str
    ) -> None:
        conn.execute("UPDATE epochs SET state=? WHERE epoch=?", (state, epoch))

    # ---- derived state reads ---------------------------------------------

    def get_checkpoint(self, epoch: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM checkpoints WHERE epoch=?", (epoch,)
        ).fetchone()

    def get_conflict(self, epoch: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM conflicts WHERE epoch=?", (epoch,)
        ).fetchone()

    def get_equivocations(self, epoch: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM equivocations WHERE epoch=? ORDER BY validator", (epoch,)
        ).fetchall()
