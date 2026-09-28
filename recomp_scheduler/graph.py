"""计算图：结构、校验与静态属性。

:class:`Graph` 是一个有向无环图（DAG），节点由唯一字符串 id 标识，边为
"输入 -> 消费者"。构建时完成全部结构校验，任何不合法都抛
:class:`~recomp_scheduler.errors.GraphValidationError`（category=input_error），
因此下游模块可以假设图是良构的。

关键静态属性
------------
- :attr:`Node.activation_elements`  输出激活大小（元素数，内存统一单位）；
- :attr:`Node.saved_elements`       前向为反向保留的额外缓冲区（如 mask）；
- :attr:`Node.workspace_elements` / :attr:`Node.backward_workspace_elements`
                                    前/反向临时工作区；
- :attr:`Graph.consumers`           每个节点的消费者列表——共享子图即
                                    ``len(consumers[v]) > 1``，也是引用计数的依据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import GraphValidationError
from .ops import ParamSpec, get_spec


@dataclass
class Node:
    id: str
    op: str
    inputs: tuple[str, ...]
    attrs: dict[str, Any] = field(default_factory=dict)
    shape: tuple[int, ...] = ()
    params: tuple[ParamSpec, ...] = ()

    # ---- 静态内存属性（由 Graph 构建期填充） ----
    @property
    def activation_elements(self) -> int:
        if self.op == "input":
            return 0
        return _prod(self.shape)

    @property
    def saved_elements(self) -> int:
        if self.op == "input":
            return 0
        spec = get_spec(self.op)
        return spec.saved_elements(self.attrs, self.shape)

    @property
    def workspace_elements(self) -> int:
        if self.op == "input":
            return 0
        return get_spec(self.op).workspace(self.attrs, self.shape)

    @property
    def backward_workspace_elements(self) -> int:
        if self.op == "input":
            return 0
        return get_spec(self.op).backward_workspace(self.attrs, self.shape)

    @property
    def is_random(self) -> bool:
        return get_spec(self.op).needs_rng

    @property
    def fanout(self) -> int:
        return len(self.consumers) if hasattr(self, "consumers") else 0


@dataclass
class Graph:
    nodes: dict[str, Node]
    order: list[str]  # 拓扑序（输入在前）
    consumers: dict[str, list[str]]
    outputs: tuple[str, ...]
    inputs: tuple[str, ...]

    # ---- 基本访问 ----
    def node(self, node_id: str) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError:
            raise GraphValidationError("unknown node", node=node_id)

    def predecessors(self, node_id: str) -> list[str]:
        return list(self.node(node_id).inputs)

    def successors(self, node_id: str) -> list[str]:
        return list(self.consumers.get(node_id, ()))

    def is_shared(self, node_id: str) -> bool:
        """共享子图节点：被超过一个消费者使用。"""
        return len(self.consumers.get(node_id, ())) > 1

    def shared_nodes(self) -> list[str]:
        return [v for v in self.order if self.is_shared(v)]

    # ---- 遍历辅助 ----
    def ancestors(self, node_id: str) -> set[str]:
        """返回 node_id 的全部祖先（不含自身）。"""
        seen: set[str] = set()
        stack = list(self.node(node_id).inputs)
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(self.node(cur).inputs)
        return seen

    def backward_order(self) -> list[str]:
        """反向计算顺序（拓扑序的逆序）。"""
        return list(reversed(self.order))


def _prod(shape: tuple[int, ...]) -> int:
    total = 1
    for s in shape:
        total *= int(s)
    return total


# --------------------------------------------------------------------- #
# 构建器
# --------------------------------------------------------------------- #
def build_graph(
    raw_nodes: list[dict[str, Any]],
    *,
    outputs: list[str] | None = None,
) -> Graph:
    """由原始字典列表构建并校验计算图。

    每个 raw node 形如::

        {"id": "h1", "op": "linear", "inputs": ["x"], "attrs": {"out_features": 4}}

    校验内容：id 唯一且为非空字符串；inputs 必须引用先于当前节点定义的节点
    （因此 raw_nodes 本身须为拓扑序）；算子存在；输入数量匹配；形状可推断；
    输出节点存在。
    """
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise GraphValidationError("graph must contain at least one node")

    nodes: dict[str, Node] = {}
    order: list[str] = []
    consumers: dict[str, list[str]] = {}

    for index, raw in enumerate(raw_nodes):
        node = _build_node(raw, index, nodes)
        if node.id in nodes:
            raise GraphValidationError("duplicate node id", node=node.id)
        for inp in node.inputs:
            consumers.setdefault(inp, []).append(node.id)
        nodes[node.id] = node
        order.append(node.id)

    _validate_acyclic(nodes, order)

    if outputs is None:
        out_ids = tuple(v for v in order if v not in consumers)
        if not out_ids:
            raise GraphValidationError("cannot infer graph outputs (every node consumed)")
    else:
        out_ids = tuple(outputs)
        for o in out_ids:
            if o not in nodes:
                raise GraphValidationError("declared output does not exist", node=o)

    input_ids = tuple(v for v, n in nodes.items() if n.op == "input")
    graph = Graph(
        nodes=nodes,
        order=order,
        consumers=consumers,
        outputs=out_ids,
        inputs=input_ids,
    )
    # 让 Node.fanout 可用
    for n in nodes.values():
        object.__setattr__(n, "consumers", consumers.get(n.id, []))
    _validate_reachable(graph)
    return graph


def _validate_reachable(graph: Graph) -> None:
    """每个非输入节点必须能到达某个声明输出：不允许不可达的死节点。

    死节点（无消费者且不是输出）会让梯度是否应当回传产生歧义；显式拒绝，
    错误类别为 input_error。
    """
    needed: set[str] = set()
    stack = list(graph.outputs)
    while stack:
        cur = stack.pop()
        if cur in needed:
            continue
        needed.add(cur)
        stack.extend(graph.node(cur).inputs)
    dead = [v for v in graph.order if v not in needed]
    if dead:
        raise GraphValidationError(
            "nodes not reachable from declared outputs (dead nodes are not allowed)",
            dead_nodes=dead,
            outputs=list(graph.outputs),
        )


def _build_node(raw: Any, index: int, prior: dict[str, Node]) -> Node:
    if not isinstance(raw, dict):
        raise GraphValidationError("node must be an object", index=index)
    node_id = raw.get("id")
    if not isinstance(node_id, str) or not node_id:
        raise GraphValidationError("node id must be a non-empty string", index=index)
    op = raw.get("op")
    if not isinstance(op, str):
        raise GraphValidationError("node op must be a string", node=node_id)
    spec = get_spec(op)  # 未知算子 -> GraphValidationError

    raw_inputs = raw.get("inputs", [])
    if not isinstance(raw_inputs, list) or not all(
        isinstance(x, str) for x in raw_inputs
    ):
        raise GraphValidationError(
            "inputs must be a list of node-id strings", node=node_id
        )
    if op != "input" and not raw_inputs:
        raise GraphValidationError("non-input node requires inputs", node=node_id)
    if op == "input" and raw_inputs:
        raise GraphValidationError("input node must have no inputs", node=node_id)
    for inp in raw_inputs:
        if inp not in prior:
            raise GraphValidationError(
                "input edge references unknown or later node "
                "(raw nodes must be in topological order)",
                node=node_id,
                edge=inp,
            )

    if spec.n_inputs >= 0 and len(raw_inputs) != spec.n_inputs:
        raise GraphValidationError(
            f"op {op!r} expects {spec.n_inputs} inputs, got {len(raw_inputs)}",
            node=node_id,
        )

    attrs = raw.get("attrs") or {}
    if not isinstance(attrs, dict):
        raise GraphValidationError("attrs must be an object", node=node_id)

    in_shapes = [prior[i].shape for i in raw_inputs]
    try:
        if op == "add" or op == "mul":
            if in_shapes and in_shapes[0] != in_shapes[1]:
                raise GraphValidationError(
                    f"op {op!r} requires equal input shapes",
                    node=node_id,
                    shapes=[tuple(s) for s in in_shapes],
                )
        shape = spec.out_shape(attrs, in_shapes)
    except GraphValidationError:
        raise
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise GraphValidationError(
            f"shape inference failed for {node_id!r}: {exc}",
            node=node_id,
            op=op,
        ) from exc

    if not shape or any(int(s) <= 0 for s in shape):
        raise GraphValidationError(
            "inferred shape must have positive dimensions",
            node=node_id,
            shape=tuple(int(s) for s in shape),
        )

    param_specs = tuple(spec.param_specs(attrs, in_shapes, shape))
    return Node(
        id=node_id,
        op=op,
        inputs=tuple(raw_inputs),
        attrs=dict(attrs),
        shape=tuple(int(s) for s in shape),
        params=param_specs,
    )


def _validate_acyclic(nodes: dict[str, Node], order: list[str]) -> None:
    """raw 列表要求拓扑序；此处再做一次 DFS 以稳妥地拒绝环。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {v: WHITE for v in nodes}

    def visit(v: str) -> None:
        color[v] = GRAY
        for u in nodes[v].inputs:
            if color[u] == GRAY:
                raise GraphValidationError("cycle detected in graph", node=u)
            if color[u] == WHITE:
                visit(u)
        color[v] = BLACK

    for v in order:
        if color[v] == WHITE:
            visit(v)
