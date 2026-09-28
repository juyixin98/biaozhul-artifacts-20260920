"""独立内存参考模型（仅供测试，不导入被测的 recomp_scheduler.memory）。

与生产模拟器不同，这里用**区间覆盖法**独立计算峰值：为每一类存储确定其
存活整数时间区间 [t_start, t_end)，再在每个时间点对所有覆盖该点的区间
求和取最大。它与 ``memory.simulate``（增量事件流水账）是两套独立实现：
若两者在全部合法方案上一致，则生命周期建模得到相互印证。

它只读取图的静态声明（形状 / saved / workspace）与方案的块定义，
不调用任何生产规划/执行代码。
"""

from __future__ import annotations

from typing import Callable

from recomp_scheduler.graph import Graph, _prod

SNAPSHOT_ELEMENTS = 128 // 8 + 1  # 与生产常量独立重述：128B / 8 + 1


def independent_peak(graph: Graph, plan) -> int:
    """区间覆盖法独立求峰值（float64 元素数）。"""
    order = graph.order
    pos = {v: i for i, v in enumerate(order)}
    spans = list(plan.blocks)
    last_block = len(spans) - 1
    bidx = [plan.block_index_of(i) for i in range(len(order))]

    def act(v: str) -> int:
        return graph.node(v).activation_elements

    def shp(v: str) -> int:
        return _prod(graph.node(v).shape)

    def saved(v: str) -> int:
        return graph.node(v).saved_elements

    return _two_pass_peak(
        graph, order, pos, spans, last_block, bidx, act, shp, saved
    )


def _two_pass_peak(graph, order, pos, spans, last_block, bidx,
                   act: Callable[[str], int], shp: Callable[[str], int],
                   saved: Callable[[str], int]) -> int:
    # ---- 第一遍：为每个关键生命周期事件排定整数时间锚点 ----
    t = 0

    def nxt() -> int:
        nonlocal t
        t += 1
        return t

    fwd_act: dict[str, int] = {}
    fwd_exit: dict[int, int] = {}
    snap: dict[int, int] = {}
    for b, (s, e) in enumerate(spans):
        if b < last_block:
            snap[b] = nxt()
        for i in range(s, e + 1):
            v = order[i]
            if graph.node(v).op != "input":
                fwd_act[v] = nxt()
        if b < last_block:
            fwd_exit[b] = nxt()

    seam = nxt()
    replay_act: dict[str, int] = {}
    snapshot_freed: dict[int, int] = {}
    bwd_point: dict[str, int] = {}
    bwd_teardown: dict[str, int] = {}
    for b in range(last_block, -1, -1):
        s, e = spans[b]
        if b < last_block:
            snapshot_freed[b] = nxt()
            for i in range(s, e):
                v = order[i]
                if graph.node(v).op != "input":
                    replay_act[v] = nxt()
        for i in range(e, s - 1, -1):
            v = order[i]
            if graph.node(v).op != "input":
                bwd_point[v] = nxt()  # 反向计算点（工作区/新梯度共存）
                bwd_teardown[v] = nxt()  # 节点帧释放点
    end = nxt()

    # ---- 第二遍：组装存活区间 ----
    intervals: list[tuple[int, int, int]] = []

    # 输入 / 参数：全程驻留
    for v in graph.inputs:
        intervals.append((0, end, shp(v)))
    for v in order:
        for spec in graph.node(v).params:
            intervals.append((0, end, _prod(spec.shape)))

    # RNG 快照
    for b in snap:
        intervals.append((snap[b], snapshot_freed[b], SNAPSHOT_ELEMENTS))

    # 前向激活 / saved / 前向工作区
    for b, (s, e) in enumerate(spans):
        for i in range(s, e + 1):
            v = order[i]
            node = graph.node(v)
            if node.op == "input":
                continue
            wf = node.workspace_elements
            if wf:
                intervals.append((fwd_act[v], fwd_act[v] + 1, wf))
            internal_ckpt = b < last_block and i < e
            stop = fwd_exit[b] if internal_ckpt else bwd_teardown[v]
            intervals.append((fwd_act[v], stop, act(v)))
            if saved(v):
                intervals.append((fwd_act[v], stop, saved(v)))

    # 重放临时帧（内部节点）：重放起点 -> 该节点反向拆帧
    for v, rs in replay_act.items():
        node = graph.node(v)
        wf = node.workspace_elements
        if wf:
            intervals.append((rs, rs + 1, wf))
        intervals.append((rs, bwd_teardown[v], act(v)))
        if saved(v):
            intervals.append((rs, bwd_teardown[v], saved(v)))

    # 上游梯度槽：接缝注入 -> 输出节点拆帧
    for o in graph.outputs:
        intervals.append((seam, bwd_teardown[o], shp(o)))

    # 参数梯度：节点反向点产生，驻留到步末
    for v, bp in bwd_point.items():
        node = graph.node(v)
        for spec in node.params:
            intervals.append((bp, end, _prod(spec.shape)))
        wb = node.backward_workspace_elements
        if wb:
            intervals.append((bp, bp + 1, wb))

    # 输入/中间梯度槽：首个消费者反向点产生；图输入驻留到 end，
    # 其余在自身反向拆帧时释放。图输出复用 gout 槽，不另建。
    gacc_start: dict[str, int] = {}
    for b in range(last_block, -1, -1):
        s, e = spans[b]
        for i in range(e, s - 1, -1):
            v = order[i]
            node = graph.node(v)
            if node.op == "input":
                continue
            for u in node.inputs:
                if u in graph.outputs:
                    continue
                gacc_start.setdefault(u, bwd_point[v])
    for u, st0 in gacc_start.items():
        stop = end if u in graph.inputs else bwd_teardown[u]
        intervals.append((st0, stop, shp(u)))

    # ---- 逐整数时间点求最大覆盖和 ----
    peak = 0
    for point in range(0, end + 1):
        total = 0
        for a, z, size in intervals:
            if a <= point < z:
                total += size
        peak = max(peak, total)
    return peak
