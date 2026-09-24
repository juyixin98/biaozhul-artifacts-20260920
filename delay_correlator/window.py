"""分块窗口工具：把长信号切成重叠或不重叠的分析窗口。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = ["Window", "iter_windows"]


@dataclass(frozen=True)
class Window:
    """一个分析窗口的起止样本号（左闭右开）。"""

    index: int
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


def iter_windows(
    length: int,
    window_size: int | None,
    hop: int | None = None,
    *,
    min_partial: float = 0.5,
) -> list[Window]:
    """生成窗口列表。

    参数
    ----
    length:
        信号总样本数。
    window_size:
        窗口长度（样本）。None 表示整段单窗口。
    hop:
        跳跃步长；None 表示不重叠（hop = window_size）。
    min_partial:
        末尾不足一个窗口时，剩余部分达到 ``window_size`` 的该比例才保留
        （默认 0.5）；比例以下的零头丢弃。整段比一个窗口短时仍作为单窗口保留。

    窗口总是成对切 A、B 两个通道，因此这里只按一个长度划分。
    """
    if length <= 0:
        return []
    if window_size is None or window_size >= length:
        return [Window(0, 0, length)]
    if window_size <= 0:
        raise ValueError("window_size 必须为正整数或 None")
    hop = hop if hop is not None else window_size
    if hop <= 0:
        raise ValueError("hop 必须为正整数")

    windows: list[Window] = []
    start = 0
    idx = 0
    while start < length:
        end = min(start + window_size, length)
        actual = end - start
        # 整窗直接收；末尾零头按 min_partial 比例决定是否保留。
        if actual == window_size or actual >= min_partial * window_size:
            windows.append(Window(idx, start, end))
            idx += 1
        if end == length:
            break
        start += hop
    return windows
