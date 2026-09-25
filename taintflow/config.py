"""分析配置：源/清洗器/汇的名称，以及有限上下文的界限。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet


DEFAULT_SOURCES = ("source",)
DEFAULT_SINKS = ("sink",)
DEFAULT_SANITIZERS = ("clean",)


@dataclass(frozen=True)
class AnalysisConfig:
    """污点分析的可调参数。

    sources/sanitizers/sinks 为内置过程名，可在 JSON 请求中自定义。

    - ``context_k``: 调用串（call string）保留长度。``k`` 之后的上下文被
      合并，这是刻意的有限上下文近似（见 README“保守近似”）。
    - ``max_trace``: 输出路径（推导轨迹）的最大步数，超出后追加截断标记。
      轨迹仅用于展示，不影响不动点的有限性。
    - ``conservative_unknown_calls``: 调用未定义函数时是否按“可能传播所有
      污点参数”处理。默认开启（保守，宁可误报不可漏报）。
    """

    sources: FrozenSet[str] = field(default_factory=lambda: frozenset(DEFAULT_SOURCES))
    sanitizers: FrozenSet[str] = field(default_factory=lambda: frozenset(DEFAULT_SANITIZERS))
    sinks: FrozenSet[str] = field(default_factory=lambda: frozenset(DEFAULT_SINKS))
    context_k: int = 2
    max_trace: int = 40
    conservative_unknown_calls: bool = True

    @classmethod
    def from_dict(cls, d: dict | None) -> "AnalysisConfig":
        d = d or {}

        def names(key: str, default: tuple[str, ...]) -> FrozenSet[str]:
            if key not in d or d[key] is None:
                return frozenset(default)
            v = d[key]
            if isinstance(v, str):
                return frozenset({v})
            return frozenset(str(x) for x in v)

        cfg = cls(
            sources=names("sources", DEFAULT_SOURCES),
            sanitizers=names("sanitizers", DEFAULT_SANITIZERS),
            sinks=names("sinks", DEFAULT_SINKS),
        )
        # dataclass(frozen=True) 上对个别字段使用 object.__setattr__
        if "context_k" in d and d["context_k"] is not None:
            object.__setattr__(cfg, "context_k", int(d["context_k"]))
        if "max_trace" in d and d["max_trace"] is not None:
            object.__setattr__(cfg, "max_trace", int(d["max_trace"]))
        if "conservative_unknown_calls" in d and d["conservative_unknown_calls"] is not None:
            object.__setattr__(
                cfg, "conservative_unknown_calls", bool(d["conservative_unknown_calls"])
            )
        if cfg.context_k < 0:
            raise ValueError("context_k 必须 >= 0")
        if cfg.max_trace < 1:
            raise ValueError("max_trace 必须 >= 1")
        return cfg
