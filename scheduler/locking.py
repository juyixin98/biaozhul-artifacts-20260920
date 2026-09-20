"""
调度互斥锁。

多个调度器（多进程 / 多线程 / 多副本）可能并发触发调度。为保证
“不超卖 GPU、不重复分配、不越过资源池配额”，每个资源池的调度
循环必须串行化：

- MySQL：使用命名咨询锁 GET_LOCK('scheduler_pool_<id>', <timeout>)，
  跨进程/跨连接互斥；锁在事务提交后、连接归还时显式释放。
- SQLite：用进程内可重入线程锁模拟（SQLite 无法跨进程咨询锁），
  足以在单测中验证并发正确性；生产用 MySQL。

配合对 pool / node / job 行的 SELECT ... FOR UPDATE 行锁，形成
“先池锁、后行锁”的固定加锁顺序，避免超卖与死锁。
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

from django.db import connection

_LOCK_PREFIX = "mlops_sched_pool_"

# SQLite（或其它无咨询锁后端）使用的进程内锁池。
_thread_locks: dict[int, threading.RLock] = {}
_thread_locks_guard = threading.Lock()


def _supports_advisory_lock() -> bool:
    return connection.vendor == "mysql"


def _thread_lock_for(pool_id: int) -> threading.RLock:
    with _thread_locks_guard:
        lock = _thread_locks.get(pool_id)
        if lock is None:
            lock = threading.RLock()
            _thread_locks[pool_id] = lock
        return lock


@contextmanager
def pool_scheduling_lock(pool_id: int, timeout_seconds: float = 10.0):
    """
    获取某资源池的调度互斥锁。

    获取失败抛 SchedulingLockBusy，调用方可直接放弃本轮调度稍后重试，
    而不是绕过锁继续分配。
    """
    if _supports_advisory_lock():
        lock_name = f"{_LOCK_PREFIX}{pool_id}"
        # MySQL 锁名最长 64 字符；名称已足够短。
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        acquired = False
        with connection.cursor() as cur:
            while True:
                wait_remaining = deadline - time.monotonic()
                if wait_remaining < 0:
                    break
                cur.execute(
                    "SELECT GET_LOCK(%s, %s)",
                    [lock_name, max(wait_remaining, 0.001)],
                )
                row = cur.fetchone()
                if row and row[0] == 1:
                    acquired = True
                    break
                # GET_LOCK 返回 0 表示超时，NULL 表示出错；都按繁忙处理。
                break
        if not acquired:
            raise SchedulingLockBusy(pool_id)
        try:
            yield
        finally:
            with connection.cursor() as cur:
                cur.execute("SELECT RELEASE_LOCK(%s)", [lock_name])
    else:
        lock = _thread_lock_for(pool_id)
        acquired = lock.acquire(timeout=timeout_seconds)
        if not acquired:
            raise SchedulingLockBusy(pool_id)
        try:
            yield
        finally:
            lock.release()


class SchedulingLockBusy(Exception):
    """资源池调度锁被其它调度器持有。"""

    def __init__(self, pool_id: int):
        super().__init__(f"scheduling lock for pool {pool_id} is busy")
        self.pool_id = pool_id
