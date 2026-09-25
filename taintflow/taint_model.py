"""污点格、符号路径轨迹与过程内数据流。

污点模型
========
一个污点标记 :class:`Taint` = ``(origin, trace)``：

- ``origin`` 唯一定位一次 ``source()`` 调用 ``(函数, 块, 指令序号)``。
  数据流的相等/连接只按 ``origin``，故格为有限幂集格，不动点必然终止。
- ``trace`` 是一条**符号轨迹**（tuple[Fragment]），用于展示源到汇的路径：
    * 首片段 :class:`SrcFrag` —— 污点源自被调函数内部的某个 source；
    * 首片段 :class:`ParFrag` —— 污点经由某个实参（origin）从调用者传入；
    * 其后为一串 :class:`StepFrag`（函数内的具体传播步骤，均带源码位置）。
  符号轨迹在函数边界由 analyzer 做 *实例化*：把 ``ParFrag`` 替换成调用者实参
  的真实轨迹 + “实参→形参”步骤。这样摘要可以按 origin 形状复用，而输出的
  路径依然逐行列连贯。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from .config import AnalysisConfig
from .ir import BasicBlock, IRFunction, Operand

Origin = Tuple[str, str, int]


# ---------------- 轨迹 ----------------


@dataclass(frozen=True)
class Step:
    """源到汇路径上的一个具体步骤（实例化后）。"""

    kind: str  # source | param_bind | copy | binop | unop | call_return | sink
    desc: str
    line: int
    col: int
    function: str
    extra: str = ""

    def as_dict(self) -> dict:
        d = {"kind": self.kind, "desc": self.desc,
             "at": f"{self.function}:{self.line}:{self.col}"}
        if self.extra:
            d["detail"] = self.extra
        return d


@dataclass(frozen=True)
class SrcFrag:
    """轨迹前缀：污点来自函数内部 source 指令。"""

    origin: Origin
    step: Step


@dataclass(frozen=True)
class ParFrag:
    """轨迹前缀：污点来自被调函数的第 ``arg_index`` 个形参。

    - ``arg_index``：β-归约键——在调用点用第该个实参的轨迹替换此前缀；
    - ``origin``：数据流身份（幂集格按它去重），等于实参污点的根 origin 形状；
    - ``bind_step``：归约时插在“实参轨迹”与“函数内后缀”之间的绑定步骤。

    归约是纯局部操作：``_transfer`` 处理 call 指令时，实参轨迹在 caller
    现场即可获得，故任意嵌套深度都能逐层正确展开，无需跨摘要查询。
    """

    arg_index: int
    origin: Origin
    bind_step: Step


@dataclass(frozen=True)
class StepFrag:
    step: Step


Fragment = SrcFrag | ParFrag | StepFrag


@dataclass(frozen=True)
class Taint:
    origin: Origin
    trace: Tuple[Fragment, ...] = ()

    def with_trace(self, trace: Tuple[Fragment, ...]) -> "Taint":
        return Taint(self.origin, trace)


def _cap_frags(trace: Sequence[Fragment], cfg: AnalysisConfig) -> Tuple[Fragment, ...]:
    if len(trace) <= cfg.max_trace:
        return tuple(trace)
    last = trace[-1]
    line = getattr(last, "step", last).line
    col = getattr(last, "step", last).col
    fn = getattr(last, "step", last).function
    marker = StepFrag(Step("truncated",
                           f"……轨迹超过 {cfg.max_trace} 步已截断（仅展示，分析未截断）",
                           line, col, fn))
    return tuple(trace[: cfg.max_trace - 1]) + (marker,)


def beta_reduce(
    trace: Sequence[Fragment],
    args: Dict[int, Dict[Origin, Tuple[Fragment, ...]]],
    cfg: AnalysisConfig,
) -> Tuple[Fragment, ...]:
    """在**一个**调用点对被调摘要轨迹做一层 β-归约。

    ``args[i]`` 是调用点第 i 个实参槽位上“origin -> caller 现场轨迹”的映射
    （一个实参变量可能携带多个不同 source origin 的污点，故逐 origin 取轨迹）。

    规则：首片段为 ``ParFrag(i, origin, bind)`` 时，替换为

        args[i][origin] + bind + 其余后缀

    关键：``args[i][origin]``（实参轨迹）内部**可能也以 ParFrag 开头**，但那
    个 ParFrag 属于**更外层**函数的形参，不能用当前调用点的 ``args`` 再归约
    （否则同一环境下形参轨迹会自我替换、无限增长）。故本函数只替换一层；
    嵌套调用时每层调用点各自做一次 beta_reduce，链自然展开。

    首片段为 :class:`SrcFrag` 时根已落地。槽位/ origin 缺失（k 限定合并掉的
    上下文，或该实参不带污点）时保留 ParFrag 前缀，由更外层处理。
    """
    if not trace:
        return ()
    head = trace[0]
    if isinstance(head, SrcFrag):
        return tuple(trace)
    if isinstance(head, ParFrag):
        slot = args.get(head.arg_index)
        actual = slot.get(head.origin) if slot is not None else None
        if not actual:
            return tuple(trace)  # 本调用点无法解析，保持符号
        return _cap_frags(
            tuple(actual) + (StepFrag(head.bind_step),) + tuple(trace[1:]),
            cfg,
        )
    return tuple(trace)


def root_origin(trace: Sequence[Fragment]) -> Optional[Origin]:
    """实例化轨迹最外层的 source origin（首个 SrcFrag）；无根则 None。"""
    for f in trace:
        if isinstance(f, SrcFrag):
            return f.origin
    return None


def concretize(trace: Sequence[Fragment]) -> List[Step]:
    """把片段轨迹压成 Step 列表（用于输出）。调用前轨迹应已以 SrcFrag 落地。"""
    return [f.step for f in trace]


# ---------------- 状态格 ----------------

State = Dict[str, FrozenSet[Taint]]


def join_state(a: State, b: State) -> State:
    """控制流汇聚（∪）。同一 origin 保留较短轨迹作为代表。"""
    out: State = {}
    for var in set(a) | set(b):
        ta = a.get(var, frozenset())
        tb = b.get(var, frozenset())
        merged: Dict[Origin, Taint] = {}
        for t in list(ta) + list(tb):
            old = merged.get(t.origin)
            if old is None or len(t.trace) < len(old.trace):
                merged[t.origin] = t
        out[var] = frozenset(merged.values())
    return out


# ---------------- 告警 ----------------


@dataclass(frozen=True)
class AlertKey:
    fn_key: str
    sink_label: str
    sink_index: int
    origin: Origin


@dataclass
class Alert:
    origin: Origin
    trace: List[Step]
    sink_fn: str
    sink_line: int
    sink_col: int
    sink_name: str
    context: Tuple[str, ...]
    classification: str = "true_positive"
    classification_note: str = ""
    may_be_false_positive: bool = False
    fp_tag: str = "definite"  # 'definite' | 'branch_overapprox'（去重用）


# ---------------- 过程内分析接口 ----------------


@dataclass
class CallResult:
    returns: FrozenSet[Taint]
    # (AlertKey, 符号轨迹, sink_name, context, 'definite'|'branch_overapprox')
    alerts: List[Tuple[AlertKey, List[Fragment], str, Tuple[str, ...], str]]
    reused: bool = False
    is_unknown: bool = False


class CalleeResolver:
    """调用点解析接口；过程间实现见 analyzer.InterproceduralAnalyzer。"""

    current_caller_key: str = ""

    def resolve(
        self,
        caller_fn: str,
        call_index: int,
        callee: str,
        arg_taints: List[FrozenSet[Taint]],
        context: Tuple[str, ...],
    ) -> CallResult:
        raise NotImplementedError

    def must_returns(
        self,
        caller_fn: str,
        call_index: int,
        callee: str,
        arg_must: List[FrozenSet[Origin]],
        context: Tuple[str, ...],
    ) -> FrozenSet[Origin]:
        """与 resolve_call 同形：被调函数在“实参必然带相应 origin”的前提下
        必然返回的 origin 集合。供 may/must 确认遍跨函数保持 must 精度。"""
        raise NotImplementedError


@dataclass
class IntraResult:
    returns: FrozenSet[Taint]
    alerts: List[Tuple[AlertKey, List[Fragment], str, Tuple[str, ...], str]]
    iterations: int
    callee_keys: List[Tuple[str, bool, str, List[FrozenSet[Taint]], Tuple[str, ...]]]
    ret_must: FrozenSet[Origin] = frozenset()
    # alerts 末位标记:
    #   'definite'         may/must 确认污点在所有终止路径到达（或其不确定性
    #                      仅来自跨递归调用的 must 精度损失——may 仍正确）
    #   'branch_overapprox' 污点只在部分分支路径存活（条件清洗类，FP-2）
    # callee_keys: (callee_name, is_unknown, call_site_name, arg_taints, context)


def _operand_taints(op: Operand, state: State) -> FrozenSet[Taint]:
    if op.kind == "const":
        return frozenset()
    return state.get(op.value, frozenset())


def _extend(t: Taint, frag: Fragment, cfg: AnalysisConfig) -> Taint:
    return Taint(t.origin, _cap_frags(t.trace + (frag,), cfg))


def _collect(parts: Sequence[FrozenSet[Taint]], frag: Optional[Fragment],
             cfg: AnalysisConfig) -> FrozenSet[Taint]:
    merged: Dict[Origin, Taint] = {}
    for part in parts:
        for t in part:
            nt = _extend(t, frag, cfg) if frag is not None else t
            old = merged.get(nt.origin)
            if old is None or len(nt.trace) < len(old.trace):
                merged[nt.origin] = nt
    return frozenset(merged.values())


def _opname(op: Operand) -> str:
    if op.kind == "const":
        return repr(op.value)
    return str(op.value)


def analyze_intraprocedural(
    ir_fn: IRFunction,
    param_taints: List[FrozenSet[Taint]],
    context: Tuple[str, ...],
    cfg: AnalysisConfig,
    resolver: CalleeResolver,
) -> IntraResult:
    """CFG 混沌迭代的过程内不动点分析。

    入口块：形参得到污点。调用者传入的参数污点 trace 在此被替换为以
    :class:`ParFrag` 开头的符号轨迹（实参 origin 记录在片段中），保证摘要
    可按 origin 形状复用。
    """
    init: State = {}
    for i, (param, taints) in enumerate(zip(ir_fn.params, param_taints)):
        symbolic = set()
        for t in taints:
            bind = Step("param_bind", f"实参绑定到形参 {param}",
                        ir_fn.name_span.start.line, ir_fn.name_span.start.col,
                        ir_fn.name, extra=param)
            symbolic.add(Taint(t.origin, (ParFrag(i, t.origin, bind),)))
        init[param] = frozenset(symbolic)

    entry_label = ir_fn.order[0]
    preds = {
        lbl: [p for p in ir_fn.order if lbl in ir_fn.blocks[p].successors]
        for lbl in ir_fn.order
    }
    block_out: Dict[str, State] = {lbl: {} for lbl in ir_fn.order}
    snapshot: dict = {}
    iterations = 0
    callee_keys: List[Tuple[str, bool, str, List[FrozenSet[Taint]], Tuple[str, ...]]] = []

    while True:
        iterations += 1
        returns: Dict[Origin, Taint] = {}
        alerts: List[Tuple[AlertKey, List[Fragment], str, Tuple[str, ...], str]] = []

        for label in ir_fn.order:
            if label == entry_label:
                state: State = {k: v for k, v in init.items()}
                for p in preds[label]:
                    state = join_state(state, block_out[p])
            else:
                state = {}
                for p in preds[label]:
                    state = join_state(state, block_out[p])

            bb = ir_fn.blocks[label]
            call_counter: Dict[Tuple[str, str, int], int] = {}
            for idx, ins in enumerate(bb.instructions):
                state, more_ret, more_alerts, callees = _transfer(
                    ir_fn, bb, idx, ins, state, context, cfg, resolver, call_counter
                )
                for rt in more_ret:
                    old = returns.get(rt.origin)
                    if old is None or len(rt.trace) < len(old.trace):
                        returns[rt.origin] = rt
                alerts.extend(more_alerts)
                callee_keys.extend(callees)
            block_out[label] = state

        new_snap = {
            lbl: {v: frozenset(t.origin for t in ts) for v, ts in st.items()}
            for lbl, st in block_out.items()
        }
        if new_snap == snapshot:
            break
        snapshot = new_snap
        if iterations > 10000:
            break

    # ---- 确定/过近似确认遍（may/must，仅 origin 集合）----
    # 主不动点回答“某 origin 是否可能到达”。这里再做一次轻量数据流回答
    # “是否所有到达本点的路径都带着它”：
    #   may  —— 某条 CFG 路径上存在（∪ 合并，含循环 0..n 次的近似）
    #   must —— 每条到达本点的路径上都存在（∩ 合并；需要“已定义”掩码，
    #            避免把未初始化变量误当成 must）
    # sink 的污点 origin 若 ∈ may 但 ∉ must，则它只在部分路径存活——
    # 典型即“一个分支清洗、另一分支不清洗”的条件清洗（README FP-2）。
    # 本遍不跨调用：调用返回值的 must 一律置空（调用内部语义不在此判定），
    # 这只会使标记更保守（把部分确定项标成过近似），不会漏报。
    sink_must, ret_must, recur_unsound = _may_must_sinks(
        ir_fn, init, preds, cfg, resolver, context
    )

    tagged = []
    for key, trace, sink_name, ctx, tag in alerts:
        if tag == "definite_local":
            if key.origin in sink_must.get(
                (key.sink_label, key.sink_index), frozenset()
            ):
                new_tag = "definite"
            else:
                # must 不成立。两种可能：
                #  (a) 分支/循环路径分叉 —— 条件清洗类过近似（FP-2）；
                #  (b) must 的精确性跨递归调用丢失（recur_unsound），但 may
                #      分析仍然正确。(b) 不应把真实告警降成误报，故仍标 definite：
                #      may（可达性）才是告警依据，must 仅用于解释 FP-2。
                if key.origin in recur_unsound:
                    new_tag = "definite"
                else:
                    new_tag = "branch_overapprox"
        else:
            new_tag = tag  # 来自被调函数摘要，标记已确定
        tagged.append((key, trace, sink_name, ctx, new_tag))

    return IntraResult(
        frozenset(returns.values()), tagged, iterations, callee_keys, ret_must,
    )


def _may_must_sinks(ir_fn, init: State, preds: Dict[str, List[str]],
                    cfg: AnalysisConfig, resolver: "CalleeResolver",
                    context: Tuple[str, ...]):
    """对 sink 指令入口与 ret 操作数计算 must-origin 集合。

    每个变量携带两个集合：``may``（可能带的 origin）与 ``must``（必然带的
    origin）。块出口在前驱上：may 取 ∪、must 取 ∩。调用点的 must 由过程间
    分析器根据被调摘要的 ``ret_must`` 注入（见 analyzer._must_returns）。

    返回 ``(sink_must, ret_must)``：
      sink_must[(block, idx)] -> 该 sink 污点参数上必然存在的 origin
      ret_must                -> 本函数所有 ret 路径上必然返回的 origin 集合
    """
    # var -> (may:set, must:set)。关键约定：键存在（即使两个集合都空）表示
    # 变量在该点“已定义但干净”；字典本身为空表示“该块本轮尚未到达”。
    block_state: Dict[str, Dict[str, tuple]] = {lbl: {} for lbl in ir_fn.order}
    reached: Dict[str, bool] = {lbl: False for lbl in ir_fn.order}
    init_sets = {
        v: (set(t.origin for t in ts), set(t.origin for t in ts))
        for v, ts in init.items()
    }
    # 每个 ret 指令所在（块,序号）-> 该 ret 操作数上的 must 集
    ret_must_at: Dict[Tuple[str, int], FrozenSet[Origin]] = {}
    sink_must: Dict[Tuple[str, int], FrozenSet[Origin]] = {}
    # origin 集合：其 must 精确性因“调用摘要未就绪/递归未解析”而丢失
    recur_unsound: set = set()
    snapshot = None

    for _ in range(10000):
        recur_unsound = set()
        for label in ir_fn.order:
            live_preds = [p for p in preds[label] if reached[p]]
            if label == ir_fn.order[0]:
                st = {v: (set(m), set(u)) for v, (m, u) in init_sets.items()}
                for p in live_preds:  # while 回边指向 head（通常非 entry）
                    st = _join_mm_real(st, block_state[p])
            elif live_preds:
                # 只合并“已到达”的前驱；首轮未到达的前驱（{}）不参与，
                # 但已到达且把变量清洗为干净 (set(),set()) 的前驱正常参与 ∩。
                st = {}
                for p in live_preds:
                    st = _join_mm_real(st, block_state[p])
            else:
                st = {}  # 不可达块：保持未到达
            reached[label] = label == ir_fn.order[0] or bool(live_preds)

            # 块入口环境（join 后）。块内顺序指令在此环境上演进；未被某条
            # 指令重定义的变量保持不变（见 _mm_transfer 对“非定义指令”的处理）。
            bb = ir_fn.blocks[label]
            call_counter_mm: Dict[Tuple[str, str, int], int] = {}
            for idx, ins in enumerate(bb.instructions):
                if ins.op == "sink":
                    x = ins.operands[0]
                    if x.kind == "var" and x.value in st:
                        sink_must[(label, idx)] = frozenset(st[x.value][1])
                    else:
                        sink_must[(label, idx)] = frozenset()
                elif ins.op == "ret":
                    if ins.operands:
                        x = ins.operands[0]
                        ret_must_at[(label, idx)] = (
                            frozenset(st[x.value][1])
                            if x.kind == "var" and x.value in st else frozenset()
                        )
                    else:
                        ret_must_at[(label, idx)] = frozenset()
                _mm_transfer(ins, st, ir_fn, bb, idx, cfg, resolver,
                             context, call_counter_mm, recur_unsound)
            block_state[label] = st

        snap = (
            tuple(
                (lbl, v, frozenset(m), frozenset(u))
                for lbl, vs in block_state.items() for v, (m, u) in vs.items()
            ),
            tuple(sorted((k, v) for k, v in sink_must.items())),
            tuple(sorted((k, v) for k, v in ret_must_at.items())),
        )
        if snap == snapshot:
            break
        snapshot = snap
        sink_must = {}
        ret_must_at = {}

    ret_must = _terminating_ret_must(ir_fn, preds, ret_must_at)
    return sink_must, frozenset(ret_must), frozenset(recur_unsound)


def _terminating_ret_must(ir_fn, preds, ret_must_at) -> set:
    """计算“函数每条终止路径都必然返回的 origin”。

    朴素地把所有 ret 块的 must 交集是错的：不同分支块中的 ret 位于**互斥**
    路径上（then 一个、else 一个），每条实际路径只经过其中一个。这里按 CFG
    结构递归：

    - ret 块：该 ret 的 must 集；
    - 无条件跳转/单后继块：后继结果；
    - br 块（then/else 两后继）：两子路径结果取 ∩（两条路径都必须带污）；
    - 汇聚块（join，多前驱但自身无分支）：按其唯一的控制流来源处理；由于本
      IR 中分支直接跳到 join，join 前各路径已在各自终点（ret 或 jump）决定，
      故对“通过 join 继续的路径”取 join 后变量状态——但 ret 已在分支内返回，
      不会落到 join 的 ret，因此 join 若含 ret，其前驱是正常的多路径 ∩。

    循环回边可能使终止性不可判定；遇回边（后向边）按保守处理（贡献空集）。
    """
    order_index = {lbl: i for i, lbl in enumerate(ir_fn.order)}

    import sys
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 5000))

    def block_must(label: str, on_path: frozenset) -> set:
        if label in on_path:
            return set()  # 后向边：可能不终止/无限循环，保守空
        bb = ir_fn.blocks[label]
        # 该块是否含 ret
        rets = [(i, ins) for i, ins in enumerate(bb.instructions) if ins.op == "ret"]
        term = bb.instructions[-1] if bb.instructions else None
        if rets:
            # 块内通常至多一条可达 ret（其后是死块）；取第一条
            i, _ins = rets[0]
            return set(ret_must_at.get((label, i), frozenset()))
        if term is None:
            return set()
        if term.op == "jump":
            return block_must(term.targets[0], on_path | {label})
        if term.op == "br":
            t = block_must(term.targets[0], on_path | {label})
            f = block_must(term.targets[1], on_path | {label})
            return t & f
        # 末尾无显式 ret 的块（隐式 return 空值）：无污点
        return set()

    return block_must(ir_fn.order[0], frozenset())


def _join_mm_real(a: Dict[str, tuple], b: Dict[str, tuple]) -> Dict[str, tuple]:
    """合并两个**都已到达**前驱的出口状态。

    状态字典中键存在即“已定义”（值可以是 (set(), set()) —— 干净）。故：

      may  = may₁ ∪ may₂
      must = must₁ ∩ must₂  —— 变量只在一条路径上定义时，该点存在一条不带它
             的路径，must 自然为空；但只要两条路径都把它定义为带同一 origin，
             即使其中一条是经清洗再赋值等情形，也按交集严格处理。

    “前驱尚未到达”的情况由调用方用 reached 掩码排除，不进入本函数。
    """
    if not a:
        return {v: (set(m), set(u)) for v, (m, u) in b.items()}
    if not b:
        return {v: (set(m), set(u)) for v, (m, u) in a.items()}
    out: Dict[str, tuple] = {}
    for var in set(a) | set(b):
        if var in a and var in b:
            out[var] = (set(a[var][0]) | set(b[var][0]),
                        set(a[var][1]) & set(b[var][1]))
        else:
            present = a.get(var, b.get(var))
            out[var] = (set(present[0]), set())
    return out


def _mm_transfer(ins, st: Dict[str, tuple], ir_fn, bb, idx,
                 cfg, resolver, context, call_counter, recur_unsound: set) -> None:
    def mm_of(op):
        if op.kind != "var" or op.value not in st:
            return set(), set()
        return set(st[op.value][0]), set(st[op.value][1])

    if ins.op in ("const", "sanitize"):
        st[ins.dst] = (set(), set())
    elif ins.op == "source":
        origin = (ir_fn.name, bb.label, idx)
        st[ins.dst] = ({origin}, {origin})
    elif ins.op == "copy":
        st[ins.dst] = mm_of(ins.operands[0])
    elif ins.op in ("binop", "unop"):
        # may：任一操作数可能带的 origin 之并。
        # must：只对**可能携带污点的变量操作数**取交集（常量从不带污点，
        # 不能参与交集，否则 x + 1 会被错误地清空 must）。
        may, must = set(), None
        for o in ins.operands:
            m, u = mm_of(o)
            may |= m
            if o.kind == "var":
                must = set(u) if must is None else (must & set(u))
        st[ins.dst] = (may, must if must is not None else set())
    elif ins.op == "call":
        may, must = set(), set()
        arg_may, arg_must = [], []
        for o in ins.operands:
            m, u = mm_of(o)
            may |= m
            arg_may.append(frozenset(m))
            arg_must.append(frozenset(u))
        # 跨调用 must：询问被调摘要“在这些实参都确定带污时是否必然返回污点”
        try:
            site = (ir_fn.name, bb.label, idx)
            call_counter[site] = call_counter.get(site, 0) + 1
            must = set(resolver.must_returns(
                ir_fn.name, call_counter[site], ins.call_name or "?",
                arg_must, context,
            ))
            # 实参可能带污但 must 查询为空：调用内部/递归的确定性未完全解析。
            # 这些 origin 的“必然”性无法确认，记为递归/调用过近似来源。
            if not must:
                for m in arg_may:
                    recur_unsound |= set(m)
        except (NotImplementedError, AttributeError, KeyError):
            must = set()
            for m in arg_may:
                recur_unsound |= set(m)
        st[ins.dst] = (may, must)
    # sink / jump / br / ret 不产生新变量



def _transfer(
    ir_fn: IRFunction,
    bb: BasicBlock,
    idx: int,
    ins,
    state: State,
    context: Tuple[str, ...],
    cfg: AnalysisConfig,
    resolver: CalleeResolver,
    call_counter: Dict[Tuple[str, str, int], int],
):
    returns: List[Taint] = []
    alerts: List[Tuple[AlertKey, List[Fragment], str, Tuple[str, ...]]] = []
    callees: List[Tuple[str, bool, str, List[FrozenSet[Taint]], Tuple[str, ...]]] = []
    span = ins.span

    def step(kind: str, desc: str, extra: str = "") -> StepFrag:
        return StepFrag(Step(kind, desc, span.start.line, span.start.col,
                             ir_fn.name, extra=extra))

    if ins.op == "const":
        state = dict(state)
        state[ins.dst] = frozenset()
    elif ins.op == "copy":
        src = ins.operands[0]
        taints = _operand_taints(src, state)
        if src.kind == "var" and src.value != ins.dst:
            taints = _collect([taints],
                              step("copy", f"赋值传播: {src.value} → {ins.dst}"), cfg)
        state = dict(state)
        state[ins.dst] = taints
    elif ins.op == "binop":
        l, r = ins.operands
        taints = _collect(
            [_operand_taints(l, state), _operand_taints(r, state)],
            step("binop", f"运算 {ins.binop} 传播",
                 extra=f"{_opname(l)} {ins.binop} {_opname(r)}"),
            cfg,
        )
        state = dict(state)
        state[ins.dst] = taints
    elif ins.op == "unop":
        x = ins.operands[0]
        taints = _collect(
            [_operand_taints(x, state)],
            step("unop", f"一元运算 {ins.unop} 传播"), cfg,
        )
        state = dict(state)
        state[ins.dst] = taints
    elif ins.op == "source":
        origin: Origin = (ir_fn.name, bb.label, idx)
        s = Step("source", "污点入口 source()",
                 span.start.line, span.start.col, ir_fn.name)
        state = dict(state)
        state[ins.dst] = frozenset({Taint(origin, (SrcFrag(origin, s),))})
    elif ins.op == "sanitize":
        # 清洗：输出恒为干净；入参轨迹在此终止（不向前传播）。
        state = dict(state)
        state[ins.dst] = frozenset()
    elif ins.op == "sink":
        x = ins.operands[0]
        for t in _operand_taints(x, state):
            s = Step("sink", f"到达汇 {ins.call_name}()",
                     span.start.line, span.start.col, ir_fn.name, extra=_opname(x))
            trace = list(_cap_frags(t.trace + (StepFrag(s),), cfg))
            key = AlertKey(f"{ir_fn.name}|{','.join(context)}", bb.label, idx, t.origin)
            alerts.append((key, trace, ins.call_name or "sink", context, "definite_local"))
    elif ins.op == "call":
        arg_taints = [_operand_taints(a, state) for a in ins.operands]
        site: Origin = (ir_fn.name, bb.label, idx)
        call_counter[site] = call_counter.get(site, 0) + 1
        result = resolver.resolve(
            ir_fn.name, call_counter[site], ins.call_name or "?",
            arg_taints, context,
        )
        callees.append((ins.call_name or "?", result.is_unknown,
                        ins.call_name or "?", list(arg_taints), context))
        # β-归约环境：形参索引 -> {origin: caller 现场轨迹}
        args_env: Dict[int, Dict[Origin, Tuple[Fragment, ...]]] = {}
        for i, ats in enumerate(arg_taints):
            if ats:
                args_env[i] = {at.origin: at.trace for at in ats}
        # 被调函数内部的 sink 告警：把其符号轨迹在本调用点归约
        for key, sym_trace, sink_name, callee_ctx, tag in result.alerts:
            inst = list(beta_reduce(sym_trace, args_env, cfg))
            if inst and isinstance(inst[0], SrcFrag):
                alerts.append((key, inst, sink_name, callee_ctx, tag))
        # 返回值：归约后再补“调用返回”步骤
        ret_step = step(
            "call_return", f"调用 {ins.call_name}() 返回污点值",
            extra=f"调用点 {ir_fn.name}:{span.start.line}",
        )
        unknown_step = StepFrag(Step(
            "unknown_propagation",
            f"经过未定义函数 {ins.call_name}()，保守视为传播污点",
            span.start.line, span.start.col, ir_fn.name,
            extra=ins.call_name or "",
        ))
        ret: Dict[Origin, Taint] = {}
        if result.is_unknown:
            # 未知函数：返回值直接取任一污点实参，打 unknown 标记
            for ats in arg_taints:
                for t in ats:
                    nt = Taint(t.origin,
                               _cap_frags(t.trace + (unknown_step, ret_step), cfg))
                    old = ret.get(nt.origin)
                    if old is None or len(nt.trace) < len(old.trace):
                        ret[nt.origin] = nt
        else:
            for t in result.returns:
                inst = beta_reduce(t.trace, args_env, cfg)
                # 归约后可能以 SrcFrag（被调内部 source）或 ParFrag（污点来自
                # 本函数形参，继续向更外层符号传播）开头；两者都接受。
                if not inst:
                    continue
                nt = Taint(t.origin, _cap_frags(inst + (ret_step,), cfg))
                old = ret.get(nt.origin)
                if old is None or len(nt.trace) < len(old.trace):
                    ret[nt.origin] = nt
        state = dict(state)
        state[ins.dst] = frozenset(ret.values())
    elif ins.op in ("jump", "br", "ret"):
        if ins.op == "ret" and ins.operands:
            for t in _operand_taints(ins.operands[0], state):
                returns.append(t)
    else:  # pragma: no cover
        raise AssertionError(f"未知 IR 指令 {ins.op}")

    return state, returns, alerts, callees
