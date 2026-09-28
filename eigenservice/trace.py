"""关联请求身份的结构化日志。

每个请求持有一个 :class:`RequestLog`，记录关键步骤、耗时、处理位置
(模块/版本) 以及单列的失败原因与不确定结论。日志通过标准 logging 输出,
同时结构化地随响应返回 (``trace`` 字段), 保证结果可解释。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import CORE_IMPL, __version__

logger = logging.getLogger("eigenservice")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(request_id)s] %(message)s",
            defaults={"request_id": "-"},
        )
    )
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


@dataclass
class RequestLog:
    """单次请求的结构化轨迹。"""

    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    steps: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    uncertainties: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.perf_counter)
    closed: bool = False

    def step(self, name: str, **detail: Any) -> None:
        entry = {
            "step": name,
            "elapsed_ms": round((time.perf_counter() - self.started_at) * 1000, 3),
            "detail": detail,
        }
        self.steps.append(entry)
        logger.info("%s %s", name, detail, extra={"request_id": self.request_id})

    def fail(self, code: str, message: str, **detail: Any) -> None:
        entry = {"code": code, "message": message, "detail": detail}
        self.failures.append(entry)
        logger.warning("failure: %s %s %s", code, message, detail,
                       extra={"request_id": self.request_id})

    def uncertain(self, reason: str, **detail: Any) -> None:
        entry = {"reason": reason, "detail": detail}
        self.uncertainties.append(entry)
        logger.warning("uncertain: %s %s", reason, detail,
                       extra={"request_id": self.request_id})

    def trace(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "service_version": __version__,
            "core": CORE_IMPL,
            "location": f"{__name__}",
            "elapsed_ms": round((time.perf_counter() - self.started_at) * 1000, 3),
            "steps": self.steps,
            "failures": self.failures,
            "uncertainties": self.uncertainties,
        }
