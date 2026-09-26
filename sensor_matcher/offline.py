"""离线配对的权威定义（offline definition）。

给定两条消息流（时间戳已校正到统一时基）与容差 ``tolerance``，定义如下：

1. **候选边**：所有满足 ``|t_b - t_a| <= tolerance`` 的 (a, b) 对构成
   加权二分图候选边，边权为时间距离 ``|dt|``。
2. **最近匹配（贪心最近邻）**：按边权升序逐条考虑；边权相同时，时间戳
   较早者优先（先按 ``min(t_a, t_b)``，再按 ``max(t_a, t_b)`` 兜底），
   仍相同则按两端 id 的字典序，保证结果完全确定、与输入顺序无关。
3. **单次消费（1:1）**：一条边被接受后，其两端消息立即标记为已消费，
   之后任何涉及它们的候选边一律跳过。每条消息最多出现在一个配对中。
4. 该贪心是按上述全局顺序的最近邻匹配；当容差远小于消息间隔时等价于
   每个消息取容差内最近对端。离线视角知道全部消息，因此作为在线算法
   的对照基准（oracle）。

返回 :class:`OfflineResult`，含配对结果与全部未配对消息。
"""

from dataclasses import dataclass

import numpy as np

from .models import Match, Message


@dataclass(frozen=True)
class OfflineResult:
    matches: tuple[Match, ...]
    unmatched_a: tuple[str, ...]
    unmatched_b: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "matches": [m.as_dict() for m in self.matches],
            "unmatched": {
                "a": list(self.unmatched_a),
                "b": list(self.unmatched_b),
            },
        }


def _edge_sort_key(edge: tuple[float, float, float, str, str]):
    """候选边的全局确定性顺序。

    edge = (abs_dt, t_a, t_b, id_a, id_b)
    距离 -> 较早时间戳 min -> 另一时间戳 max -> (a_id, b_id) 字典序。
    """
    abs_dt, t_a, t_b, id_a, id_b = edge
    early = min(t_a, t_b)
    late = max(t_a, t_b)
    return (abs_dt, early, late, id_a, id_b)


def offline_pair(
    stream_a: list[Message],
    stream_b: list[Message],
    tolerance: float,
    raw_a: dict[str, float] | None = None,
    raw_b: dict[str, float] | None = None,
) -> OfflineResult:
    """按离线权威定义计算 1:1 最近邻配对。

    Args:
        stream_a / stream_b: 两流消息，时间戳视为已校正。重复时间戳允许
            存在，靠 id 与平局规则消歧。
        tolerance: 非负时间容差（秒）。
        raw_a / raw_b: 可选的 id -> 原始时间戳映射，仅用于在结果中记录
            校正前时间；缺省时与校正后时间相同。

    Raises:
        ValueError: tolerance 为负，或同一流内 id 重复。
    """
    if tolerance < 0:
        raise ValueError("tolerance 不能为负")

    if len({m.id for m in stream_a}) != len(stream_a):
        raise ValueError("stream_a 中存在重复 id")
    if len({m.id for m in stream_b}) != len(stream_b):
        raise ValueError("stream_b 中存在重复 id")

    raw_a = raw_a or {}
    raw_b = raw_b or {}

    # 向量化构造候选边：|dt| <= tolerance
    ta = np.asarray([m.timestamp for m in stream_a], dtype=float)
    tb = np.asarray([m.timestamp for m in stream_b], dtype=float)
    edges: list[tuple[float, float, float, str, str]] = []
    if ta.size and tb.size:
        diff = np.abs(ta[:, None] - tb[None, :])
        ia_idx, ib_idx = np.nonzero(diff <= tolerance)
        for i, j in zip(ia_idx.tolist(), ib_idx.tolist(), strict=True):
            ma, mb = stream_a[i], stream_b[j]
            edges.append((float(diff[i, j]), ma.timestamp, mb.timestamp, ma.id, mb.id))

    edges.sort(key=_edge_sort_key)

    used_a: set[str] = set()
    used_b: set[str] = set()
    matches: list[Match] = []
    for abs_dt, t_a, t_b, id_a, id_b in edges:
        if id_a in used_a or id_b in used_b:
            continue  # 单次消费：任一端已被更早（更优或平局优先）的边占用
        used_a.add(id_a)
        used_b.add(id_b)
        matches.append(
            Match(
                id_a=id_a,
                id_b=id_b,
                t_a=t_a,
                t_b=t_b,
                raw_a=raw_a.get(id_a, t_a),
                raw_b=raw_b.get(id_b, t_b),
            )
        )

    # 配对按 A 侧时间戳排序输出，稳定易读
    matches.sort(key=lambda m: (m.t_a, m.id_a))
    unmatched_a = tuple(m.id for m in stream_a if m.id not in used_a)
    unmatched_b = tuple(m.id for m in stream_b if m.id not in used_b)
    return OfflineResult(tuple(matches), unmatched_a, unmatched_b)
