"""nonce 登记表：原子“检查并登记”。

两种实现：

* :class:`MemoryNonceStore`：单进程多线程。单一锁内完成过期清理与插入判定，
  锁不跨 I/O，临界区很短。
* :class:`SqliteNonceStore`：跨线程/跨进程。``(key_id, nonce)`` 唯一主键，
  ``BEGIN IMMEDIATE`` 事务内插入，靠数据库唯一约束裁决并发。

两者都实现同一个接口：

    claim(key_id, nonce, ts, now) -> bool   # True=首次登记成功，False=重放
    close()
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Protocol


class NonceStore(Protocol):
    def claim(self, key_id: str, nonce: str, ts: int, now: float) -> bool: ...
    def close(self) -> None: ...


class MemoryNonceStore:
    """进程内 nonce 存储，线程安全。"""

    def __init__(self, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        # (key_id, nonce) -> 该记录的过期时刻（epoch 秒）
        self._seen: dict[tuple[str, str], float] = {}

    def claim(self, key_id: str, nonce: str, ts: int, now: float) -> bool:
        # 记录保留到 “请求时间戳 + ttl”：晚到的重放即使在 ttl 边界附近到达，
        # 登记表里仍有它（详见 README 第 4 节）。
        expires_at = ts + self._ttl
        with self._lock:
            self._prune_locked(now)
            key = (key_id, nonce)
            if key in self._seen:
                return False
            self._seen[key] = float(expires_at)
            return True

    def _prune_locked(self, now: float) -> None:
        # expires_at == now 即视为已过期（与 claim 的 ttl 语义一致）。
        expired = [k for k, exp in self._seen.items() if exp <= now]
        for k in expired:
            del self._seen[k]

    def close(self) -> None:
        with self._lock:
            self._seen.clear()


class SqliteNonceStore:
    """基于 SQLite 唯一主键的 nonce 存储，可跨线程/进程使用。"""

    SCHEMA = (
        "CREATE TABLE IF NOT EXISTS used_nonces ("
        "key_id TEXT NOT NULL, "
        "nonce TEXT NOT NULL, "
        "expires_at REAL NOT NULL, "
        "PRIMARY KEY (key_id, nonce))"
    )

    def __init__(self, path: str, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = ttl_seconds
        # check_same_thread=False：由 BEGIN IMMEDIATE 事务自身保证串行裁决；
        # 每次 claim 使用独立 cursor/事务。
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(self.SCHEMA)
        self._lock = threading.Lock()
        self._conn.commit()

    def claim(self, key_id: str, nonce: str, ts: int, now: float) -> bool:
        expires_at = float(ts + self._ttl)
        # 进程内先串行化，再由数据库唯一约束兜底跨进程并发。
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "DELETE FROM used_nonces WHERE expires_at <= ?", (now,)
                )
                cur = self._conn.execute(
                    "INSERT INTO used_nonces (key_id, nonce, expires_at) "
                    "VALUES (?, ?, ?)",
                    (key_id, nonce, expires_at),
                )
                self._conn.commit()
                return cur.rowcount == 1
            except sqlite3.IntegrityError:
                # 唯一约束冲突 = 已有并发事务先登记 → 重放。
                self._conn.rollback()
                return False
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()
