"""DAWG 状态模型。

状态等价判定（契约 1）：两个状态等价当且仅当它们的**终结标记相同**
**且**带标签的转移集合完全相同。不能只按出边数量（子节点数量）合并——
出边数相同但终结标记不同、标签不同或目标不同的状态必须保持区分。
"""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

# 等价键：(终结标记, 排序后的 (符号, 目标状态 id) 序列)。
# 同时包含终结标记与转移，二者缺一不可。
StateKey = tuple[bool, tuple[tuple[str, int], ...]]


def state_key(final: bool, transitions: Mapping[str, int]) -> StateKey:
    """计算状态的规范等价键。

    终结标记与全部 (符号, 目标) 转移都参与比较。目标用状态 id 表示；
    最小化过程自底向上进行，调用时子状态均已是寄存器中的规范状态。
    """
    return (bool(final), tuple(sorted(transitions.items())))


@dataclass(frozen=True)
class State:
    """不可变自动机状态。

    :param state_id: 全局唯一 id。
    :param final: 终结标记（空词被接受时根状态为终结）。
    :param transitions: 只读映射 ``符号 -> 目标状态 id``。
    """

    state_id: int
    final: bool
    transitions: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # frozen dataclass 上用 object.__setattr__ 包一层只读视图。
        if not isinstance(self.transitions, MappingProxyType):
            object.__setattr__(
                self, "transitions", MappingProxyType(dict(self.transitions))
            )

    def key(self) -> StateKey:
        return state_key(self.final, self.transitions)

    @property
    def out_degree(self) -> int:
        return len(self.transitions)
