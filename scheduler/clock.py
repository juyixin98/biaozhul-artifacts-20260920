"""
可注入时钟。

业务代码不直接调用 timezone.now()，而是从 Clock 取当前时间；
测试可注入 FakeClock 任意推进时间，确定性地验证 2 小时排队超时、
心跳失联、运行超时等逻辑。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from django.utils import timezone


class Clock:
    """默认时钟：读取真实当前时间（UTC）。"""

    def now(self):
        return timezone.now()

    def after(self, seconds: float, *, ref=None):
        return (ref or self.now()) + timedelta(seconds=seconds)


class FakeClock(Clock):
    """测试时钟：时间只在测试显式推进时变化。"""

    def __init__(self, start=None):
        self._now = start or timezone.now()

    def now(self):
        return self._now

    def advance(self, seconds: float):
        self._now += timedelta(seconds=seconds)
        return self._now

    def set(self, value):
        self._now = value
        return self._now


# 全局默认时钟实例；测试通过 set_default_clock 注入。
_default_clock: Optional[Clock] = None


def get_clock() -> Clock:
    return _default_clock or _SYSTEM_CLOCK


def set_default_clock(clock: Optional[Clock]) -> None:
    """注入自定义时钟；传 None 恢复系统时钟。"""
    global _default_clock
    _default_clock = clock


_SYSTEM_CLOCK = Clock()
