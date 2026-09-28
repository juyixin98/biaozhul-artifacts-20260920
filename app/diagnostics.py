"""诊断：带请求标识的结构化日志与脱敏输入画像。

原则：日志里只出现计数、量级、摘要哈希与关键状态（方法、块大小、结论），
绝不打印原始数值序列 —— 输入即敏感数据。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


@dataclass
class RequestContext:
    """单请求诊断上下文：request_id 贯穿日志与响应。"""

    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: float = field(default_factory=time.perf_counter)

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started_at) * 1000.0

    def log_decision(self, logger: logging.Logger, decision: str, **state: object) -> None:
        """decision 取值：accepted / rejected / undecidable；state 只放脱敏关键状态。"""
        logger.info(
            "request_id=%s decision=%s elapsed_ms=%.2f %s",
            self.request_id,
            decision,
            self.elapsed_ms(),
            " ".join(f"{k}={v!r}" for k, v in state.items()),
        )
