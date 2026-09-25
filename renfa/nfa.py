"""Thompson 构造法：把 AST 编译成 NFA。

状态与边
--------
每个状态至多携带一条"消费类"边（Char / Any），其余为 ``ε`` 边；
锚点 ``^``/``$`` 是**条件 ε 边**——不消费码点，但仅在文本的相应位置可通过
（见 :mod:`renfa.engine` 的闭包算法）。

重复的处理
----------
``{n}``、``{n,m}`` 的有界部分通过**复制 NFA 片段**实现（每次复制都向
Builder 申请全新状态，因此子片段之间绝不共享状态）；``*``、``+``、``{n,}``
的无限部分用经典 Thompson 回环边。构造出的 NFA 状态数与重复次数成线性关系，
受 ``MAX_STATES`` 限制以防恶意/错误模式耗尽资源。

这是核心"编译器"组件，全部手写，不使用任何现成正则/编译框架。
"""

from dataclasses import dataclass, field

from . import ast_nodes as ast
from .errors import RegexCompileError

# NFA 状态数上限：{n,m} 的 m 可达 1_000_000（词法层），构造层再用状态上限兜底。
MAX_STATES = 20_000


@dataclass
class Edge:
    kind: str  # 'eps' | 'char' | 'any' | 'anchor'
    target: int
    codepoint: int | None = None  # kind == 'char' 时的码点
    anchor: str | None = None     # kind == 'anchor' 时为 '^' 或 '$'


@dataclass
class State:
    index: int
    edges: list[Edge] = field(default_factory=list)


@dataclass
class Frag:
    """Thompson 片段：有唯一入口状态与唯一出口（可接受）状态。"""

    start: int
    accept: int


class NFA:
    def __init__(self, states: list[State], start: int, accept: int):
        self.states = states
        self.start = start
        self.accept = accept

    @property
    def size(self) -> int:
        return len(self.states)

    def to_debug(self) -> dict:
        """导出为可 JSON 序列化的调试结构（状态编号与边列表）。"""
        out = {"start": self.start, "accept": self.accept, "states": []}
        for st in self.states:
            edges = []
            for e in st.edges:
                item = {"kind": e.kind, "target": e.target}
                if e.codepoint is not None:
                    item["codepoint"] = e.codepoint
                    item["char"] = chr(e.codepoint)
                if e.anchor is not None:
                    item["anchor"] = e.anchor
                edges.append(item)
            out["states"].append({"index": st.index, "edges": edges})
        return out


class Builder:
    def __init__(self, max_states: int = MAX_STATES):
        self.states: list[State] = []
        self.max_states = max_states

    def new_state(self) -> int:
        if len(self.states) >= self.max_states:
            raise RegexCompileError(
                f"NFA 状态数超过上限 {self.max_states}：请减小重复次数"
            )
        idx = len(self.states)
        self.states.append(State(idx))
        return idx

    def add_edge(self, src: int, edge: Edge) -> None:
        self.states[src].edges.append(edge)

    def eps(self, src: int, dst: int) -> None:
        self.add_edge(src, Edge("eps", dst))

    # ---- Thompson 组合原语 ----

    def compile_node(self, node: ast.Node) -> Frag:
        if isinstance(node, ast.Empty):
            s = self.new_state()
            a = self.new_state()
            self.eps(s, a)
            return Frag(s, a)
        if isinstance(node, ast.Literal):
            s = self.new_state()
            a = self.new_state()
            self.add_edge(s, Edge("char", a, codepoint=node.codepoint))
            return Frag(s, a)
        if isinstance(node, ast.AnyChar):
            s = self.new_state()
            a = self.new_state()
            self.add_edge(s, Edge("any", a))
            return Frag(s, a)
        if isinstance(node, ast.Anchor):
            s = self.new_state()
            a = self.new_state()
            self.add_edge(s, Edge("anchor", a, anchor=node.kind))
            return Frag(s, a)
        if isinstance(node, ast.Group):
            return self.compile_node(node.child)
        if isinstance(node, ast.Concat):
            return self._compile_concat(node)
        if isinstance(node, ast.Alt):
            return self._compile_alt(node)
        if isinstance(node, ast.Repeat):
            return self._compile_repeat(node)
        raise RegexCompileError(f"内部错误：未知 AST 节点 {type(node).__name__}")

    def _compile_concat(self, node: ast.Concat) -> Frag:
        if not node.children:
            s = self.new_state()
            a = self.new_state()
            self.eps(s, a)
            return Frag(s, a)
        frags = [self.compile_node(c) for c in node.children]
        for left, right in zip(frags, frags[1:]):
            # 串联：直接用 ε 连接，相邻片段可共享端点；这里保持状态独立以简化调试
            self.eps(left.accept, right.start)
        return Frag(frags[0].start, frags[-1].accept)

    def _compile_alt(self, node: ast.Alt) -> Frag:
        entry = self.new_state()
        exit_ = self.new_state()
        lf = self.compile_node(node.left)
        rf = self.compile_node(node.right)
        self.eps(entry, lf.start)
        self.eps(entry, rf.start)
        self.eps(lf.accept, exit_)
        self.eps(rf.accept, exit_)
        return Frag(entry, exit_)

    def _compile_repeat(self, node: ast.Repeat) -> Frag:
        mn, mx = node.mn, node.mx
        # 1) mn 个必需副本（每复制一次重新编译，拿到彼此独立的状态）
        entry = self.new_state()
        cur = entry
        for _ in range(mn):
            f = self.compile_node(node.child)
            self.eps(cur, f.start)
            cur = f.accept

        if mx is None:
            # {mn,}：mn 次之后，剩余是经典 Thompson 循环（入口与出口合一的 star）
            loop_in = self.new_state()
            loop_out = self.new_state()
            self.eps(cur, loop_in)
            f = self.compile_node(node.child)
            self.eps(loop_in, f.start)
            self.eps(f.accept, loop_in)   # 回环
            self.eps(loop_in, loop_out)  # 跳过（退出循环）
            return Frag(entry, loop_out)

        # 2) 有限上界：再放 (mx - mn) 个可选副本，每个都可平行 ε 跳过
        tail = cur
        for _ in range(mx - mn):
            f = self.compile_node(node.child)
            join = self.new_state()
            self.eps(tail, f.start)   # 走这个副本
            self.eps(tail, join)      # 跳过这个副本
            self.eps(f.accept, join)
            tail = join
        return Frag(entry, tail)


def build_nfa(root: ast.Node, max_states: int = MAX_STATES) -> NFA:
    b = Builder(max_states=max_states)
    frag = b.compile_node(root)
    return NFA(b.states, frag.start, frag.accept)
