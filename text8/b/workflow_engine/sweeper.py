"""超时升级后台扫描线程（不依赖消息队列/外部调度服务）。

- 独立线程 + 独立 DB 连接，周期性调用 ``run_sweep``；
- 单轮失败只记录日志，下一周期继续 —— 任务失败可重试；
- 进程重启后线程重新启动，自动继续处理未完成升级。
"""

import logging
import threading

from sqlalchemy.orm import Session

from workflow_engine.config import Settings
from workflow_engine.database import make_engine
from workflow_engine.services.templates import get_instance_definition
from workflow_engine.timeouts import run_sweep

logger = logging.getLogger("workflow_engine.sweeper")


class TimeoutSweeper:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._engine = None
        self._session_factory = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._engine = make_engine(self.settings.database_url)
        from sqlalchemy.orm import sessionmaker

        self._session_factory = sessionmaker(
            bind=self._engine, autoflush=False, expire_on_commit=False, future=True
        )
        self._thread = threading.Thread(target=self._run, name="timeout-sweeper", daemon=True)
        self._thread.start()
        logger.info("Timeout sweeper started (interval=%ss)", self.settings.sweep_interval_seconds)

    def _run(self) -> None:
        while not self._stop.wait(self.settings.sweep_interval_seconds):
            try:
                session = self._session_factory()
                try:
                    summary = run_sweep(session, get_definition=get_instance_definition)
                    if summary["processed"] or summary["failed"]:
                        logger.info("Sweep result: %s", summary)
                finally:
                    session.close()
            except Exception:
                logger.exception("Sweep iteration failed; will retry")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._engine is not None:
            self._engine.dispose()
        self._thread = None
