"""时间戳工具。

约定：模块内统一使用 **整数毫秒 epoch**（int64）表示时间，避免浮点比较带来的
边界歧义。事件时刻与数据入库时刻使用同一时基。

关键语义
--------
事件时刻 ``event_time``：业务事件真实发生的时刻（如一次点击、一笔交易）。

入库时刻 ``ingest_time``：特征记录真正写入特征库、对下游**可见**的时刻。
一条特征即使业务生效时间很早，若数据迟到（``ingest_time > event_time``），
在事件发生的那一刻它尚不可用，做离线回放时必须排除，否则就是
"事后修订泄漏"（look-ahead / revision leakage）。
"""
from __future__ import annotations

from datetime import datetime, timezone

_MS_PER_SECOND = 1_000
_MS_PER_MINUTE = 60 * _MS_PER_SECOND
_MS_PER_HOUR = 60 * _MS_PER_MINUTE
_MS_PER_DAY = 24 * _MS_PER_HOUR


def now_ms() -> int:
    """当前 UTC 时刻的整数毫秒。"""
    return int(datetime.now(tz=timezone.utc).timestamp() * _MS_PER_SECOND)


def dt(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0, ms: int = 0) -> int:
    """把 UTC 日期时间字面量转为整数毫秒，便于在测试/样例中书写可读时间。"""
    return int(
        datetime(y, m, d, hh, mm, ss, ms * 1000, tzinfo=timezone.utc).timestamp()
        * _MS_PER_SECOND
    )


def parse_iso(value: str) -> int:
    """解析 ISO-8601 字符串为整数毫秒。

    接受 ``2026-01-01T00:00:00Z``、``2026-01-01T00:00:00+00:00`` 与
    ``2026-01-01T00:00:00``（无时区时按 UTC 处理）。
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * _MS_PER_SECOND)


def to_iso(ms: int) -> str:
    """整数毫秒转回 UTC ISO-8601 字符串（毫秒精度，带 Z 后缀）。"""
    seconds, remainder = divmod(ms, _MS_PER_SECOND)
    return (
        datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        + f".{remainder:03d}Z"
    )


def minutes(n: int) -> int:
    return n * _MS_PER_MINUTE


def hours(n: int) -> int:
    return n * _MS_PER_HOUR


def days(n: int) -> int:
    return n * _MS_PER_DAY
