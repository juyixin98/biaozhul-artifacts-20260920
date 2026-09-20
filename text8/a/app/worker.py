"""超时升级后台轮询。

不依赖消息队列：单进程内守护线程周期性扫描 escalation 表。
- 处理认领使用 ``FOR UPDATE SKIP LOCKED``，多副本部署也不会重复处理；
- 单条升级失败只记录 attempts/last_error 并保留 pending，下轮重试；
- 服务重启后 due_at 已过期的升级会立即被捞起继续处理（恢复语义）。
"""
from __future__ import annotations

import logging
import threading
import time

from app.config import settings
from app.db import SessionLocal
from app.engine import process_due_escalations

logger = logging.getLogger("flow.worker")


class EscalationWorker(threading.Thread):
    daemon = True

    def __init__(self, poll_seconds: float | None = None, batch_size: int | None = None):
        super().__init__(name="escalation-worker")
        self._stop = threading.Event()
        self.poll_seconds = (
            poll_seconds if poll_seconds is not None else settings.worker_poll_seconds
        )
        self.batch_size = batch_size if batch_size is not None else settings.worker_batch_size

    def run(self) -> None:
        logger.info(
            "超时升级 worker 启动 poll=%.1fs batch=%d", self.poll_seconds, self.batch_size
        )
        while not self._stop.is_set():
            try:
                with SessionLocal() as db:
                    n = process_due_escalations(db, limit=self.batch_size)
                if n:
                    logger.info("处理了 %d 条到期升级", n)
            except Exception:  # noqa: BLE001 - 轮询循环永不退出
                logger.exception("升级轮询异常，将在下个周期重试")
            self._stop.wait(self.poll_seconds)
        logger.info("超时升级 worker 停止")

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> int:
        """供测试/脚本手动触发一次。"""
        with SessionLocal() as db:
            return process_due_escalations(db, limit=self.batch_size)
