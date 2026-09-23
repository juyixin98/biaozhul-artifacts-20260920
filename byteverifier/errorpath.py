"""最短可读错误路径。

验证器在函数 CFG 上用 Dijkstra（回边代价高于普通边，优先展示不绕循环的
路径）找到从函数入口到出错位置的最短控制流路径，再结合每条指令保存的
源码位置渲染为可读文本。

注意：这是**控制流可达**意义上的最短路径，不保证其前驱条件在具体输入下
可满足；它回答的问题是“控制如何走到这一点”，而非“什么输入能触发”。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

from .bytecode import JUMP_OPS, OP_JIF, OP_JMP, OPCODE_NAMES, CodeObject

NORMAL_COST = 1
BACK_EDGE_COST = 5      # 回边代价更高：优先给出不绕循环的解释


@dataclass
class PathStep:
    pc: int
    kind: str                 # entry / fallthrough / branch / back-edge / call-n/a
    span_line: int = 0        # 该指令源码行（0 表示无源码信息）
    text: str = ""            # 反汇编文本（无源码时用）


@dataclass
class ErrorPath:
    function: str
    error_pc: int
    steps: list[PathStep]
    note: str = ""

    def is_empty(self) -> bool:
        return not self.steps


def build_cfg(code: CodeObject) -> tuple[dict[int, list[tuple[int, str, int]]], int]:
    """返回 (edges, code_len)。

    edges[pc] = [(target_pc, kind, cost), ...]
    kind ∈ {"fallthrough", "branch", "back-edge"}
    """
    from .bytecode import OPERAND_SIZE

    by_pc = {ins.pc: ins for ins in code.instructions}
    code_len = 0
    edges: dict[int, list[tuple[int, str, int]]] = {}
    for ins in code.instructions:
        nxt = ins.pc + 1 + OPERAND_SIZE[ins.opcode]
        code_len = max(code_len, nxt)
        es: list[tuple[int, str, int]] = []
        if ins.opcode == OP_JMP:
            kind = "back-edge" if ins.operand <= ins.pc else "branch"
            cost = BACK_EDGE_COST if kind == "back-edge" else NORMAL_COST
            es.append((ins.operand, kind, cost))
        elif ins.opcode == OP_JIF:
            # 条件为假走分支目标，为真顺序执行（见 compiler 中 if/while 发射方式）
            tkind = "back-edge" if ins.operand <= ins.pc else "branch"
            tcost = BACK_EDGE_COST if tkind == "back-edge" else NORMAL_COST
            es.append((ins.operand, tkind, tcost))
            if nxt in by_pc:
                es.append((nxt, "fallthrough", NORMAL_COST))
        elif ins.opcode not in (0x51, 0x52) and nxt in by_pc:
            es.append((nxt, "fallthrough", NORMAL_COST))
        edges[ins.pc] = es
    return edges, code_len


def shortest_path(code: CodeObject, error_pc: int) -> ErrorPath:
    """入口 (pc=0) -> error_pc 的最短路径。"""
    edges, _ = build_cfg(code)
    if not code.instructions:
        return ErrorPath(code.name, error_pc, [])

    dist = {0: 0}
    prev: dict[int, tuple[int, str]] = {}
    pq = [(0, 0)]
    while pq:
        d, pc = heapq.heappop(pq)
        if d != dist.get(pc):
            continue
        if pc == error_pc:
            break
        for tgt, kind, cost in edges.get(pc, []):
            nd = d + cost
            if tgt not in dist or nd < dist[tgt]:
                dist[tgt] = nd
                prev[tgt] = (pc, kind)
                heapq.heappush(pq, (nd, tgt))

    if error_pc not in dist and error_pc != 0:
        # 错误点本身不可达（例如越界跳转目标）时，路径只给到最近可达指令
        return ErrorPath(code.name, error_pc, [])

    chain: list[tuple[int, str]] = []
    cur = error_pc
    while cur != 0:
        p, kind = prev[cur]
        chain.append((cur, kind))
        cur = p
    chain.reverse()

    by_pc = {ins.pc: ins for ins in code.instructions}
    steps = [PathStep(pc=0, kind="entry",
                      span_line=_line(code, 0),
                      text=_ins_text(code, by_pc.get(0)))]
    for tgt_pc, kind in chain:
        steps.append(PathStep(
            pc=tgt_pc, kind=kind,
            span_line=_line(code, tgt_pc),
            text=_ins_text(code, by_pc.get(tgt_pc)),
        ))
    note = ("路径为控制流图上按边数（回边权重更高）计算的最短可达路径；"
            "不代表具体运行时输入。")
    return ErrorPath(code.name, error_pc, steps, note=note)


def _line(code: CodeObject, pc: int) -> int:
    span = code.pc_spans.get(pc)
    return span.line if span else 0


def _ins_text(code: CodeObject, ins) -> str:
    if ins is None:
        return "<非指令边界>"
    name = OPCODE_NAMES.get(ins.opcode, f"0x{ins.opcode:02x}")
    from .bytecode import OPERAND_SIZE
    if OPERAND_SIZE[ins.opcode]:
        return f"{name} {ins.operand}"
    return name


def render_path(path: ErrorPath) -> str:
    """把最短路径渲染为多行可读字符串。"""
    if not path.steps:
        return (f"  [最短路径] 无法在 {path.function} 的控制流图中构造到达 "
                f"offset={path.error_pc} 的路径（目标可能不可达/越界）")
    out = [f"  [最短错误路径] 函数 {path.function}，共 {len(path.steps)} 步:"]

    def describe(step: PathStep, prev_line: int) -> str:
        loc = f"行 {step.span_line}" if step.span_line else f"offset {step.pc}"
        if step.kind == "entry":
            return f"  入口  @{loc}: {step.text}"
        arrow = {
            "fallthrough": "顺序执行",
            "branch": "条件跳转",
            "back-edge": "回边(循环)",
        }.get(step.kind, step.kind)
        same = "（同一源码行）" if prev_line and step.span_line == prev_line else ""
        return f"  -> {arrow} @{loc}{same}: {step.text}"

    prev_line = 0
    for st in path.steps:
        out.append(describe(st, prev_line))
        prev_line = st.span_line or prev_line
    if path.note:
        out.append(f"  说明: {path.note}")
    return "\n".join(out)


def error_path_to_dict(path: ErrorPath) -> dict:
    return {
        "function": path.function,
        "error_offset": path.error_pc,
        "length": len(path.steps),
        "steps": [
            {
                "offset": s.pc,
                "kind": s.kind,
                "source_line": s.span_line or None,
                "instruction": s.text,
            }
            for s in path.steps
        ],
        "note": path.note,
    }
