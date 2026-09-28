"""请求身份关联的结构化日志。

每条日志都带 request_id，关键步骤（规则、索引版本、扫描行数、
失败类别）以 key=value 形式输出，便于复现失败时定位处理位置。
"""
from __future__ import annotations

import logging
import sys

LOGGER_NAME = "collationsvc"

_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.addHandler(handler)
    logger.propagate = False
    _CONFIGURED = True


def get_logger() -> logging.Logger:
    configure_logging()
    return logging.getLogger(LOGGER_NAME)


def log_step(request_id: str, step: str, **fields) -> None:
    """记录一个关键处理步骤，字段统一为 key=value。"""
    kv = " ".join(f"{k}={v!r}" for k, v in sorted(fields.items()))
    get_logger().info("request_id=%s step=%s %s", request_id, step, kv)


def log_failure(request_id: str, category: str, message: str, **fields) -> None:
    """单列失败原因与不确定结论。"""
    kv = " ".join(f"{k}={v!r}" for k, v in sorted(fields.items()))
    get_logger().warning(
        "request_id=%s step=failure category=%s reason=%r %s",
        request_id,
        category,
        message,
        kv,
    )
