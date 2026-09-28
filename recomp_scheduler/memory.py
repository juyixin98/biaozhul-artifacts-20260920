"""内存模型与方案静态模拟器。

给定良构 :class:`~recomp_scheduler.graph.Graph` 与一个合法检查点方案
(:class:`~recomp_scheduler.planner.Plan`)，:func:`simulate` 以抽象张量槽位
重放整个训练步（前向 -> 反向）的分配/释放事件，给出：

- ``peak_elements``       预测峰值（保留激活 + 保留 saved 缓冲 + 梯度累加 +
                          临时工作区，全部以 float64 元素数计）；
- ``seam_retained``       前向结束/反向开始之间必须保留的内容分项；
- ``recompute_flops``     额外重算量（每个被检查点化的块在反向时整块重放一次；
                          最后一个块不重放）；
- ``events``              事件时间线（测试用独立模拟器据此/独立区间法交叉对账）。

约定（与 ops 注册表的静态属性一一对应）：

- 图输入与参数全程驻留；块边界激活（块末节点输出）驻留到最后一个消费者块处理完；
- 被检查点化块的内部激活与 saved 缓冲在前向离开该块时释放，反向处理该块时
  从边界激活 + RNG 快照整块重放重建；
- 最后一个块不重放：其激活/saved 从前向保留到反向；
- 图输出必须位于最后一块或本身是块边界（否则输出值无法保留，方案非法）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .graph import Graph, _prod

# RNG 快照以 pickle bytes 保存（PCG64 状态约 124 字节），
# 折算为 float64 元素单位计入预算（保守向上取整 +1）。
SNAPSHOT_BYTES = 128
SNAPSHOT_ELEMENTS = SNAPSHOT_BYTES // 8 + 1


@dataclass(frozen=True)
class Event:
    delta: int
    key: str
    kind: str  # activation | saved | workspace | gradient | param | input | snapshot | external


@dataclass
class MemoryProfile:
    peak_elements: int
    peak_live_without_workspace: int
    seam_retained: dict[str, int]
    recompute_flops: int
    forward_flops: int
    backward_flops: int
    events: tuple[Event, ...] = field(repr=False)
    peak_at_event: int = field(repr=False, default=0)
    timeline: tuple[tuple[int, int, str], ...] = field(repr=False, default=())

    @property
    def total_flops(self) -> int:
        return self.forward_flops + self.backward_flops + self.recompute_flops

    @property
    def recompute_ratio(self) -> float:
        base = self.forward_flops + self.backward_flops
        return self.recompute_flops / base if base else 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "peak_elements": self.peak_elements,
            "peak_live_without_workspace": self.peak_live_without_workspace,
            "seam_retained": dict(self.seam_retained),
            "recompute_flops": self.recompute_flops,
            "forward_flops": self.forward_flops,
            "backward_flops": self.backward_flops,
            "total_flops": self.total_flops,
            "recompute_ratio": round(self.recompute_ratio, 6),
        }


def block_of(plan, order: list[str]) -> list[int]:
    """节点拓扑序位置 -> 块编号。"""
    ends = set(plan.boundary_positions)
    blocks: list[int] = []
    cur = 0
    for pos in range(len(order)):
        blocks.append(cur)
        if pos in ends:
            cur += 1
    return blocks


def node_flops(node, phase: str) -> int:
    """算子的粗略 FLOP 估计（仅用于明确额外计算量，不参与内存账）。"""
    n = _prod(node.shape)
    if node.op == "linear":
        in_features, out_features = node.params[0].shape
        batch = n // max(out_features, 1)
        fwd = 2 * batch * in_features * out_features
        return fwd if phase == "forward" else 2 * fwd
    if node.op in ("relu", "tanh", "dropout"):
        return n
    if node.op in ("add", "mul"):
        return n if phase == "forward" else 2 * n
    return 0


# --------------------------------------------------------------------- #
# 事件模拟器
# --------------------------------------------------------------------- #
def simulate(graph: Graph, plan) -> MemoryProfile:
    order = graph.order
    n = len(order)
    bidx = block_of(plan, order)
    pos = {v: i for i, v in enumerate(order)}
    last_block = max(bidx)

    events: list[Event] = []

    def emit(delta: int, key: str, kind: str) -> None:
        events.append(Event(delta, key, kind))

    # 每个块的位置区间
    spans: list[tuple[int, int]] = []
    for b in range(last_block + 1):
        ps = [i for i, x in enumerate(bidx) if x == b]
        spans.append((ps[0], ps[-1]))

    # ---- 前向开始：输入 + 参数驻留 ----
    for v in graph.inputs:
        emit(+_prod(graph.node(v).shape), f"input:{v}", "input")
    param_total = 0
    for v in order:
        for spec in graph.node(v).params:
            param_total += _prod(spec.shape)
            emit(+_prod(spec.shape), f"param:{v}:{spec.name}", "param")

    def act_size(v: str) -> int:
        return graph.node(v).activation_elements

    def saved_size(v: str) -> int:
        return graph.node(v).saved_elements

    # ---- 前向逐块 ----
    for b, (s, e) in enumerate(spans):
        checkpointed = b < last_block
        if checkpointed:
            emit(+SNAPSHOT_ELEMENTS, f"snapshot:{b}", "snapshot")
        for i in range(s, e + 1):
            v = order[i]
            if graph.node(v).op != "input":
                wf = graph.node(v).workspace_elements
                if wf:
                    emit(+wf, f"ws_fwd:{v}", "workspace")
                emit(+act_size(v), f"act:{v}", "activation")
                if saved_size(v):
                    emit(+saved_size(v), f"saved:{v}", "saved")
                if wf:
                    emit(-wf, f"ws_fwd:{v}", "workspace")
        if checkpointed:
            # 离开块：内部节点的 saved 与全部内部激活释放；
            # 块末（边界）节点的激活与其自身 saved 缓冲保留——
            # 它的反向需要这些 saved，而激活是后续块唯一的入口。
            for i in range(s, e):
                v = order[i]
                if saved_size(v):
                    emit(-saved_size(v), f"saved:{v}", "saved")
                emit(-act_size(v), f"act:{v}", "activation")
            # act:order[e] 与 saved:order[e] 保留

    # ---- 前向/反向接缝：记录驻留分项 ----
    seam_index = len(events)

    # ---- 反向开始：外部上游梯度 ----
    for o in graph.outputs:
        emit(+act_size(o), f"gout:{o}", "external")

    # 边界激活的存活规则：前向保留 -> 被后续（逆序中更早处理的）消费块使用 ->
    # 在其所属块自身的反向拆帧时释放（边界节点自己的反向仍需要它的 act/saved）。
    # 因此模拟器中边界槽只有一个，无需"按最后消费块释放"；引用计数的多持有方
    # 语义由引擎在运行时维护（每个后续消费块一个 retain，拆帧时各释放一次）。

    gacc_started: set[str] = set()

    def grad_size(v: str) -> int:
        # 输入节点 activation_elements 为 0，但其梯度尺寸等于输入形状。
        return _prod(graph.node(v).shape)

    def start_gacc(u: str) -> None:
        # 图输出的梯度槽就是反向开始时注入的 gout，不重复计数。
        if u in gacc_started or u in graph.outputs:
            gacc_started.add(u)
            return
        gacc_started.add(u)
        kind = "gradient"
        emit(+grad_size(u), f"gacc:{u}", kind)

    # ---- 反向逐块（逆序） ----
    for b in range(last_block, -1, -1):
        s, e = spans[b]
        replayed = b < last_block
        if replayed:
            # 恢复 RNG 快照
            emit(-SNAPSHOT_ELEMENTS, f"snapshot:{b}", "snapshot")
            # 重放内部节点（s..e-1）：临时重建内部激活与 saved；
            # 边界节点 e 的激活/saved 是前向保留的，不重放。
            for i in range(s, e):
                v = order[i]
                if graph.node(v).op == "input":
                    continue
                wf = graph.node(v).workspace_elements
                if wf:
                    emit(+wf, f"ws_re:{v}", "workspace")
                emit(+act_size(v), f"ract:{v}", "activation")
                if saved_size(v):
                    emit(+saved_size(v), f"rsaved:{v}", "saved")
                if wf:
                    emit(-wf, f"ws_re:{v}", "workspace")

        # 块内逆序反向：节点级立即分配/释放（与真实 autodiff 及引擎一致）。
        for i in range(e, s - 1, -1):
            v = order[i]
            node = graph.node(v)
            if node.op == "input":
                continue
            is_boundary = replayed and i == e
            wb = node.backward_workspace_elements
            # 反向计算期间：工作区 + 本节点帧(act/saved/grad) + 新产生的梯度共存。
            if wb:
                emit(+wb, f"ws_bwd:{v}", "workspace")
            for spec in node.params:
                emit(+_prod(spec.shape), f"pgrad:{v}:{spec.name}", "gradient")
            for u in node.inputs:
                start_gacc(u)
            if wb:
                emit(-wb, f"ws_bwd:{v}", "workspace")
            # 反传结束：本节点的输出梯度立即被消费，saved 与激活立即释放。
            if v in graph.outputs:
                emit(-grad_size(v), f"gout:{v}", "external")
            else:
                emit(-grad_size(v), f"gacc:{v}", "gradient")
            if is_boundary:
                if saved_size(v):
                    emit(-saved_size(v), f"saved:{v}", "saved")
                emit(-act_size(v), f"act:{v}", "activation")
            elif replayed:
                if saved_size(v):
                    emit(-saved_size(v), f"rsaved:{v}", "saved")
                emit(-act_size(v), f"ract:{v}", "activation")
            else:
                if saved_size(v):
                    emit(-saved_size(v), f"saved:{v}", "saved")
                emit(-act_size(v), f"act:{v}", "activation")

    # 图输出激活（最后一块中或边界）均已在所属块拆帧时释放；
    # 下方 end_live 平账会再次确认没有遗漏。

    # ---- 步末：输出激活、输入梯度、参数梯度移交后释放；输入/参数卸载 ----
    # 输出激活：若仍在账上（输出为块边界的情况）则释放
    end_live = _end_keys(events)
    for o in graph.outputs:
        if f"act:{o}" in end_live:
            emit(-act_size(o), f"act:{o}", "activation")
            end_live.discard(f"act:{o}")
    # 输入梯度作为结果移交
    for v in graph.inputs:
        key = f"gacc:{v}"
        if key in end_live:
            emit(-grad_size(v), key, "gradient")
            end_live.discard(key)
    # 参数梯度移交
    for v in order:
        for spec in graph.node(v).params:
            key = f"pgrad:{v}:{spec.name}"
            if key in end_live:
                emit(-_prod(spec.shape), key, "gradient")
                end_live.discard(key)
    # 卸载输入与参数
    for key in list(end_live):
        if key.startswith("input:"):
            v = key.split(":", 1)[1]
            emit(-_prod(graph.node(v).shape), key, "input")
            end_live.discard(key)
        elif key.startswith("param:"):
            _, v, pname = key.split(":")
            spec = next(s for s in graph.node(v).params if s.name == pname)
            emit(-_prod(spec.shape), key, "param")
            end_live.discard(key)

    if end_live:
        raise AssertionError(f"planner ledger not balanced: {sorted(end_live)}")

    # ---- 汇总峰值/分项 ----
    live = 0
    peak = 0
    peak_non_ws = 0
    live_non_ws = 0
    peak_idx = -1
    timeline: list[tuple[int, int, str]] = []
    for idx, ev in enumerate(events):
        if ev.kind == "workspace":
            live += ev.delta
        else:
            live_non_ws += ev.delta
            live += ev.delta
        if live > peak:
            peak, peak_idx = live, idx
        peak_non_ws = max(peak_non_ws, live_non_ws)
        timeline.append((live, live_non_ws, ev.key))

    seam = _seam_breakdown(events, seam_index)

    fwd_flops = sum(node_flops(graph.node(v), "forward") for v in order)
    bwd_flops = sum(node_flops(graph.node(v), "backward") for v in order)
    re_flops = 0
    for b, (s, e) in enumerate(spans):
        if b < last_block:
            # 边界节点 e 不重放（其激活/saved 前向保留）。
            for i in range(s, e):
                v = order[i]
                if graph.node(v).op != "input":
                    re_flops += node_flops(graph.node(v), "forward")

    return MemoryProfile(
        peak_elements=peak,
        peak_live_without_workspace=peak_non_ws,
        seam_retained=seam,
        recompute_flops=re_flops,
        forward_flops=fwd_flops,
        backward_flops=bwd_flops,
        events=tuple(events),
        peak_at_event=peak_idx,
        timeline=tuple(timeline),
    )


def _end_keys(events: list[Event]) -> set[str]:
    live: set[str] = set()
    for ev in events:
        if ev.delta > 0:
            live.add(ev.key)
        else:
            live.discard(ev.key)
    return set(live)


def _seam_breakdown(events: list[Event], seam_index: int) -> dict[str, int]:
    totals: dict[str, int] = {}
    live_keys: dict[str, int] = {}
    for idx, ev in enumerate(events):
        if idx >= seam_index:
            break
        if ev.delta > 0:
            live_keys[ev.key] = (ev.kind, ev.delta)
        else:
            live_keys.pop(ev.key, None)
    for kind, delta in live_keys.values():
        totals[kind] = totals.get(kind, 0) + delta
    return totals
