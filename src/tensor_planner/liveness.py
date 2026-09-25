"""活跃区间（liveness）分析。

两级区间：

1. **值级区间** :func:`value_intervals`
   每个值 ``v`` 的半开整数区间 ``[birth, last_use]``。约定在时刻 ``t``
   执行的 op 可以读取在 ``t`` 时刻死亡的值（死亡发生在该 op 读完之后），
   因此两个区间端点相接（前一个 ``end == t``、后一个 ``start == t``）
   不算同时存活——区间重叠判定用严格重叠 ``a.start < b.end and b.start < a.end``。

2. **存储级区间** :func:`storage_intervals`
   slice 视图与其基底共享同一块物理存储，必须合并为一个存储组
   （根为基底）。存储组的区间 = 成员值区间的并集上的最小外包
   ``[min birth, max last_use]``；由于视图是基底的纯别名（不复制数据），
   任何成员存活都意味着整块基底存储存活。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from .dag import DAG


@dataclass(frozen=True)
class LiveInterval:
    """半开活跃区间 ``[start, end)``，单位为 op 执行时刻。"""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start

    def overlaps(self, other: "LiveInterval") -> bool:
        """严格重叠：端点相接（a.end == b.start）不算重叠。"""
        return self.start < other.end and other.start < self.end

    def contains_time(self, time: int) -> bool:
        return self.start <= time < self.end


def value_intervals(dag: DAG) -> Dict[str, LiveInterval]:
    """计算每个值的活跃区间。

    - 输入：``[0, last_use]``；
    - op 输出：``[op.time, last_use]``（last_use 即最后一个消费者的时刻；
      没有消费者时区间退化为 ``[time, time]``，但仍是必须分配的输出）。
    """
    intervals: Dict[str, LiveInterval] = {}
    for name in dag.values:
        birth = dag.birth(name)
        death = dag.last_use(name)
        if death < birth:  # 理论上不会发生，防御性检查
            death = birth
        intervals[name] = LiveInterval(birth, death)
    return intervals


def storage_intervals(
    dag: DAG, value_iv: Dict[str, LiveInterval] | None = None
) -> Dict[str, LiveInterval]:
    """计算每个存储组根（物理缓冲区）的活跃区间。

    返回字典的键是存储组根名（普通值根即自身；切片组根为基底名）。
    """
    if value_iv is None:
        value_iv = value_intervals(dag)

    result: Dict[str, LiveInterval] = {}
    for root in dag.all_storage_roots():
        members = dag.members_of(root)
        start = min(value_iv[m].start for m in members)
        end = max(value_iv[m].end for m in members)
        result[root] = LiveInterval(start, end)
    return result


def live_storage_at(
    storage_iv: Dict[str, LiveInterval], time: int
) -> List[str]:
    """在给定时刻仍存活的存储组根（含恰在该时刻出生的，不含已结束的）。"""
    return sorted(
        root
        for root, iv in storage_iv.items()
        if iv.start <= time < iv.end
    )


def peak_concurrency(
    storage_iv: Dict[str, LiveInterval],
) -> Tuple[int, int]:
    """返回 (最大同时存活存储组数, 出现该峰值的最小时刻)。

    端点相接不重复计数：在时刻 ``t``，区间满足 ``start <= t < end``。
    """
    if not storage_iv:
        return 0, 0
    points = sorted(
        {iv.start for iv in storage_iv.values()}
        | {iv.end for iv in storage_iv.values()}
    )
    peak = 0
    peak_time = points[0]
    for t in points:
        count = sum(1 for iv in storage_iv.values() if iv.start <= t < iv.end)
        if count > peak:
            peak = count
            peak_time = t
    return peak, peak_time
