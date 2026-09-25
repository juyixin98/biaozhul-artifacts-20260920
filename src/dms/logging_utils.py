"""日志安全。

两层防护：

1. 所有业务代码只记录 schema 级信息（规则 id、路径、错误类型），
   从源头不把文档字段值写进日志；
2. ``SensitiveContext`` 上下文管理器把待处理文档中出现的敏感标量值登记到
   ``contextvars``，``SecretScrubFilter`` 在日志落盘前再把这些值（以及其
   JSON 序列化形式）替换为 ``***REDACTED***``，作为纵深防御。
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

REDACTION = "***REDACTED***"

_secrets: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "dms_secret_values", default=frozenset()
)


def register_sensitive_values(doc: Any) -> None:
    """登记文档中所有标量值的字符串形式（含其 JSON 编码形式）。"""
    found: set[str] = set()
    _collect(doc, found)
    _secrets.set(frozenset(found))


def clear_sensitive_values() -> None:
    _secrets.set(frozenset())


@contextmanager
def sensitive_scope(doc: Any) -> Iterator[None]:
    token = _secrets.set(_collect_to_set(doc, frozenset()))
    try:
        yield
    finally:
        _secrets.reset(token)


def _collect_to_set(doc: Any, existing: frozenset[str]) -> frozenset[str]:
    found: set[str] = set(existing)
    _collect(doc, found)
    return frozenset(found)


def _collect(node: Any, found: set[str]) -> None:
    if isinstance(node, (dict, list)):
        iterator = node.values() if isinstance(node, dict) else node
        for child in iterator:
            _collect(child, found)
    elif node is None or isinstance(node, bool):
        return
    elif isinstance(node, str):
        if node:
            found.add(node)
    else:  # int, float
        found.add(str(node))


def scrub(text: str) -> str:
    """把已登记的敏感值从任意文本中抹掉。"""
    if not text:
        return text
    values = _secrets.get()
    if not values:
        return text
    # 长值优先，避免短值先替换导致长值残留片段
    for value in sorted(values, key=len, reverse=True):
        if value and value in text:
            text = text.replace(value, REDACTION)
    return text


class SecretScrubFilter(logging.Filter):
    """logging.Filter：输出前清洗 msg / args / 常见结构化字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = scrub(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: _scrub_obj(v) for k, v in record.args.items()}
                else:
                    record.args = tuple(_scrub_obj(a) for a in record.args)
            for attr in ("details", "extra"):
                if hasattr(record, attr):
                    setattr(record, attr, _scrub_obj(getattr(record, attr)))
        except Exception:  # 日志失败绝不能影响业务路径
            return True
        return True


def _scrub_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, dict):
        return {k: _scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        cleaned = [_scrub_obj(v) for v in obj]
        return cleaned if isinstance(obj, list) else tuple(cleaned)
    return obj


def get_logger(name: str = "dms") -> logging.Logger:
    logger = logging.getLogger(name)
    if not any(isinstance(f, SecretScrubFilter) for f in logger.filters):
        logger.addFilter(SecretScrubFilter())
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.addFilter(SecretScrubFilter())
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
