"""最小无环确定自动机（MADFA / DAWG）挖掘内核。

构建算法：有序输入下的**增量状态合并**（Daciuk 等人的经典
replace-or-register 算法）。

* 输入必须字典序非递减（契约 2 由语料规范层强制，本层再做一次防御性断言）；
* 每加入一个词，先沿现有自动机走与该词的最长公共前缀，再把**上一个词**
  位于公共前缀之下的后缀自底向上做"替换或登记"：

  - 寄存器中已有等价状态：把父边重定向到该状态（合并，自动机缩小）；
  - 否则：把该状态按等价键登记；

* **等价键同时包含终结标记与全部带标签转移**（见 :mod:`app.core.state`），
  终结性不同、标签不同或目标不同的状态绝不会被错误合并（契约 1）；

* 不变量：已登记（register）状态的出边此后不再被修改——下一条词的新后缀
  只会接在公共前缀末端那个尚未登记的状态上。

构建完成后产出不可变 :class:`Dawg`，并在反向拓扑序上计算每个状态接受的
词数，用于前缀计数：

    count(s) = final(s) + Σ_{(sym,t) ∈ δ(s)} count(t)

即使两条边指向同一个共享目标状态也要各计一次：经不同符号到达的是不同的
字符串。
"""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from app.core.state import State, StateKey, state_key
from app.corpus.errors import UnorderedCorpusError


class DawgStructureError(RuntimeError):
    """自动机结构不合法（如出现环或悬空引用）。"""

    error_code = "dawg_structure_error"


class _MutableState:
    """构建期内部可变状态；构建结束后冻结为 :class:`State`。"""

    __slots__ = ("state_id", "final", "transitions")

    def __init__(self, state_id: int, final: bool = False) -> None:
        self.state_id = state_id
        self.final = final
        self.transitions: dict[str, "_MutableState"] = {}

    def key(self) -> StateKey:
        return state_key(
            self.final, {sym: child.state_id for sym, child in self.transitions.items()}
        )


@dataclass(frozen=True)
class MergeEvent:
    """一次合并（重定向到已登记等价状态）的诊断记录。"""

    symbol: str
    parent_id: int
    replaced_id: int
    equivalent_id: int


@dataclass(frozen=True)
class BuildStats:
    """构建过程的可观测统计（诊断用）。"""

    accepted_words: int
    duplicate_words: int
    raw_states_created: int
    final_state_count: int
    merge_count: int
    steps: tuple[str, ...] = field(default_factory=tuple)


class DawgBuilder:
    """有序词表 → 最小 DAWG 的增量构建器。

    本构建器不是线程安全的；按"一个语料一个构建器"使用。
    """

    def __init__(self, *, max_step_log: int = 200) -> None:
        self._root = _MutableState(0)
        self._next_id = 1
        self._register: dict[StateKey, _MutableState] = {}
        self._last_word: str | None = None
        self._accepted = 0
        self._duplicates = 0
        self._created = 1  # 根状态
        self._merges: list[MergeEvent] = []
        self._steps: list[str] = []
        self._max_step_log = max_step_log

    @property
    def stats_so_far(self) -> tuple[int, int, int]:
        return self._accepted, self._duplicates, self._created

    def add(self, word: str) -> bool:
        """加入一个词。

        :returns: ``True`` 表示新词已加入，``False`` 表示重复词被跳过。
        :raises UnorderedCorpusError: 词相对上一个输入逆序。
        """
        if not isinstance(word, str):
            raise TypeError("word 必须是 str")
        if self._last_word is not None and word < self._last_word:
            msg = (
                f"挖掘内核检测到逆序输入: {word!r} 小于上一个词 "
                f"{self._last_word!r}；DAWG 增量最小化要求有序输入"
            )
            raise UnorderedCorpusError(msg)
        if word == self._last_word:
            self._duplicates += 1
            self._log(f"skip duplicate {word!r}")
            return False

        current = self._root
        index = 0
        # 1) 沿现有结构走最长公共前缀。
        while index < len(word) and word[index] in current.transitions:
            current = current.transitions[word[index]]
            index += 1

        # 2) 把上一个词位于公共前缀之下的后缀做最小化。
        if self._last_word is not None:
            self._minimize_suffix(self._last_word, index, current)

        # 3) 为新词挂接新后缀。
        for pos in range(index, len(word)):
            child = _MutableState(self._next_id)
            self._next_id += 1
            self._created += 1
            current.transitions[word[pos]] = child
            current = child
        current.final = True

        self._accepted += 1
        self._last_word = word
        self._log(f"add word {word!r} at prefix length {index}")
        return True

    def _minimize_suffix(
        self, last_word: str, shared_depth: int, shared_state: _MutableState
    ) -> None:
        """沿上一个词在公共深度之下的后缀，自底向上替换或登记。"""
        # 收集路径 [(parent, symbol, child), ...]，严格位于公共前缀之下。
        path: list[tuple[_MutableState, str, _MutableState]] = []
        node = shared_state
        for pos in range(shared_depth, len(last_word)):
            symbol = last_word[pos]
            child = node.transitions[symbol]
            path.append((node, symbol, child))
            node = child

        for parent, symbol, child in reversed(path):
            key = child.key()
            equivalent = self._register.get(key)
            if equivalent is not None and equivalent is not child:
                parent.transitions[symbol] = equivalent
                self._merges.append(
                    MergeEvent(
                        symbol=symbol,
                        parent_id=parent.state_id,
                        replaced_id=child.state_id,
                        equivalent_id=equivalent.state_id,
                    )
                )
                self._log(
                    f"merge state {child.state_id} -> {equivalent.state_id} "
                    f"on {symbol!r} (final={key[0]}, out={len(key[1])})"
                )
            elif equivalent is None:
                self._register[key] = child
                self._log(
                    f"register state {child.state_id} "
                    f"(final={child.final}, out={len(child.transitions)})"
                )

    def _log(self, message: str) -> None:
        if len(self._steps) < self._max_step_log:
            self._steps.append(f"step#{len(self._steps) + 1}: {message}")

    def build(self) -> "Dawg":
        """结束输入：最小化最后一个词的整条后缀，并冻结为不可变模型。"""
        if self._last_word is not None:
            self._minimize_suffix(self._last_word, 0, self._root)
        return self._freeze()

    def _freeze(self) -> "Dawg":
        # _MutableState 的 id 在合并后可能不连续；按可达性重排为紧凑 id。
        #
        # 计数依赖真正的后序（子先于父）。BFS 序的逆序不够：跨层共享时
        # 一个浅状态可能被深状态引用（例如 node11 -> node3）。这里用
        # 迭代式后序 DFS，并在遍历中顺带检测环（白色 0 / 灰色 1 / 黑色 2）。
        color: dict[int, int] = {self._root.state_id: 0}
        postorder: list[_MutableState] = []
        stack: list[tuple[_MutableState, int]] = [(self._root, 0)]
        color[self._root.state_id] = 1
        while stack:
            node, edge_index = stack[-1]
            children = list(node.transitions.values())
            if edge_index < len(children):
                stack[-1] = (node, edge_index + 1)
                child = children[edge_index]
                if color.get(child.state_id, 0) == 1:
                    msg = f"检测到环：状态 {child.state_id} 已在当前 DFS 栈上"
                    raise DawgStructureError(msg)
                if child.state_id not in color:
                    color[child.state_id] = 1
                    stack.append((child, 0))
            else:
                color[node.state_id] = 2
                postorder.append(node)
                stack.pop()

        # 以根优先的发现顺序分配紧凑 id（根固定为 0）。
        remap: dict[int, int] = {self._root.state_id: 0}
        discovery: list[_MutableState] = []
        seen: set[int] = {self._root.state_id}
        dfs: list[_MutableState] = [self._root]
        while dfs:
            node = dfs.pop()
            discovery.append(node)
            for child in sorted(node.transitions.values(), key=lambda c: c.state_id):
                if child.state_id not in seen:
                    seen.add(child.state_id)
                    dfs.append(child)
                    if child.state_id not in remap:
                        remap[child.state_id] = len(remap)

        frozen: dict[int, State] = {}
        for node in discovery:
            transitions = MappingProxyType(
                {
                    symbol: remap[child.state_id]
                    for symbol, child in sorted(node.transitions.items())
                }
            )
            frozen[remap[node.state_id]] = State(
                state_id=remap[node.state_id],
                final=node.final,
                transitions=transitions,
            )

        # 后序保证：计算父计数时所有子计数已知（跨层共享也成立）。
        counts: dict[int, int] = {}
        for node in postorder:
            sid = remap[node.state_id]
            total = 1 if node.final else 0
            for child in node.transitions.values():
                total += counts[remap[child.state_id]]
            counts[sid] = total

        stats = BuildStats(
            accepted_words=self._accepted,
            duplicate_words=self._duplicates,
            raw_states_created=self._created,
            final_state_count=len(frozen),
            merge_count=len(self._merges),
            steps=tuple(self._steps),
        )
        return Dawg(
            states=frozen,
            root_id=0,
            word_counts=MappingProxyType(counts),
            stats=stats,
            merge_events=tuple(self._merges),
        )


@dataclass(frozen=True)
class Dawg:
    """不可变最小无环确定自动机。

    :param states: ``状态 id -> State``（含根 0，状态 id 紧凑）。
    :param root_id: 根状态 id（固定为 0）。
    :param word_counts: 每个状态接受的词数（含自身终结贡献）。
    """

    states: Mapping[int, State]
    root_id: int
    word_counts: Mapping[int, int]
    stats: BuildStats
    merge_events: tuple[MergeEvent, ...] = field(default_factory=tuple)

    # ---- 遍历与查询 -------------------------------------------------------

    def transition(self, state_id: int, symbol: str) -> int | None:
        state = self.states[state_id]
        return state.transitions.get(symbol)

    def _walk(self, text: str) -> int | None:
        """沿文本走自动机；中途断边返回 ``None``。"""
        current = self.root_id
        for symbol in text:
            nxt = self.transition(current, symbol)
            if nxt is None:
                return None
            current = nxt
        return current

    def contains(self, word: str) -> bool:
        """成员判定：词被自动机接受当且仅当路径终点为终结状态。"""
        end = self._walk(word)
        return end is not None and self.states[end].final

    def prefix_count(self, prefix: str) -> int:
        """返回以 ``prefix`` 为前缀的词典词数；前缀不可达时为 0。

        空前缀返回词典总词数（根状态计数）。
        """
        end = self._walk(prefix)
        if end is None:
            return 0
        return self.word_counts[end]

    # ---- 结构校验（供持久化层复用）---------------------------------------

    def all_state_ids(self) -> list[int]:
        return sorted(self.states)

    def iter_edges(self):
        """产出 ``(source, symbol, target)``，按源 id、符号排序。"""
        for sid in sorted(self.states):
            for symbol, target in sorted(self.states[sid].transitions.items()):
                yield sid, symbol, target

    def reachable_state_ids(self) -> frozenset[int]:
        """从根可达的状态 id 集合（BFS）。"""
        seen = {self.root_id}
        queue = [self.root_id]
        while queue:
            sid = queue.pop()
            for target in self.states[sid].transitions.values():
                if target not in seen:
                    seen.add(target)
                    queue.append(target)
        return frozenset(seen)

    def total_words(self) -> int:
        return self.word_counts[self.root_id]


def build_dawg(
    words: list[str],
    *,
    max_step_log: int = 200,
) -> Dawg:
    """从**已去重且有序**的词表构建最小 DAWG。"""
    builder = DawgBuilder(max_step_log=max_step_log)
    for word in words:
        builder.add(word)
    return builder.build()
