"""过程间污点分析：有限上下文摘要 + 单调不动点。

上下文
======
每个被分析的函数“实例”以下面二者区分（有限上下文，刻意不追求完全上下文敏感）：

1. **参数污点形状**：每个形参的 origin 集合。形状不同的调用（不同来源数量/组合）
   分别建摘要 —— 这是多态（polyvariant）划分，足以区分“干净实参 / 污点实参 /
   两个污点实参”等不同调用上下文。
2. **k 限定调用串** ``context``：最近 ``context_k`` 个调用点
   ``(调用者,行号,序号)``。递归超过 k 层后调用串相同，上下文被合并 —— 这是
   README 中 FP-3 所述的有意近似。

不动点
======
摘要从空（无返回污点、无告警）起步；每次重算若返回污点 origin 集合或告警键集合
增长，则把依赖该摘要的调用者重新入队。格有限且转移单调，故必然终止。

保守性
======
- 未定义函数调用（且配置允许）：返回值视为可能携带任一污点实参；
- 分支：两支都分析，汇合按 ∪ —— 条件清洗（一支洗、一支不洗）汇合后仍有污点，
  这是有意的过近似（README FP-2）；
- while：循环回边 ∪ 合并，0..n 次执行的效果都被覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple

from .config import AnalysisConfig
from .ir import IRProgram
from .taint_model import (
    Alert,
    AlertKey,
    CalleeResolver,
    CallResult,
    Fragment,
    Origin,
    ParFrag,
    SrcFrag,
    Step,
    Taint,
    analyze_intraprocedural,
    concretize,
)


@dataclass
class Summary:
    key: str
    fn_name: str
    shape: Tuple[FrozenSet[Origin], ...]
    context: Tuple[str, ...]
    returns: FrozenSet[Taint] = frozenset()
    alerts: List[Tuple[AlertKey, List[Fragment], str, Tuple[str, ...], str]] = field(
        default_factory=list
    )  # (key, 符号轨迹, sink 名, 上下文, 'definite'|'branch_overapprox')
    iterations: int = 0
    initialized: bool = False
    ret_must: FrozenSet[Origin] = frozenset()


def _shape_of(args: List[FrozenSet[Taint]], arity: int) -> Tuple[FrozenSet[Origin], ...]:
    out = []
    for i in range(arity):
        ts = args[i] if i < len(args) else frozenset()
        out.append(frozenset(t.origin for t in ts))
    return tuple(out)


def _shape_str(shape: Tuple[FrozenSet[Origin], ...]) -> str:
    return ";".join(
        "{" + ",".join(f"{a}:{b}:{i}" for a, b, i in sorted(s)) + "}" for s in shape
    )


def _ctx_str(context: Tuple[str, ...]) -> str:
    return ",".join(context)


def _push_ctx(context: Tuple[str, ...], site_id: str, k: int) -> Tuple[str, ...]:
    """追加调用点并做 k 限定截断。

    不能写 ``(context + (site,))[-k:]``：Python 中 ``-0 == 0``，``seq[-0:]``
    等于整个序列而非空序列，k=0 时会导致调用串无限增长（递归不终止）。
    """
    new = context + (site_id,)
    return new[len(new) - k:] if k > 0 else ()


class _Resolver(CalleeResolver):
    def __init__(self, analyzer: "InterproceduralAnalyzer", key: str):
        self.a = analyzer
        self.current_caller_key = key

    def resolve(
        self,
        caller_fn: str,
        call_index: int,
        callee: str,
        arg_taints: List[FrozenSet[Taint]],
        context: Tuple[str, ...],
    ) -> CallResult:
        return self.a.resolve_call(
            self.current_caller_key, caller_fn, call_index, callee,
            arg_taints, context,
        )

    def must_returns(self, caller_fn, call_index, callee, arg_must, context):
        return self.a.must_returns(
            caller_fn, call_index, callee, arg_must, context
        )


@dataclass
class AnalysisStats:
    summaries: int = 0
    reanalyses: int = 0
    intra_iterations: int = 0
    max_call_depth: int = 0


class InterproceduralAnalyzer:
    def __init__(self, program: IRProgram, config: AnalysisConfig,
                 entry: str = "main"):
        self.program = program
        self.cfg = config
        self.entry = entry
        self.summaries: Dict[str, Summary] = {}
        # summary_key -> 依赖它的调用者 key 集合
        self.dependents: Dict[str, set] = {}
        self.worklist: List[str] = []
        self.stats = AnalysisStats()
        self.unknown_callsites: set = set()

    # ---------------- 驱动 ----------------
    def run(self) -> None:
        if self.entry not in self.program.functions:
            # 无 main：以全干净参数分析每个函数（仍能发现其内部 source->sink）
            seeds = []
            for name, fn in self.program.functions.items():
                args = [frozenset() for _ in fn.params]
                seeds.append((name, args, ()))
            for name, args, ctx in seeds:
                self._enqueue(name, args, ctx)
        else:
            fn = self.program.functions[self.entry]
            args = [frozenset() for _ in fn.params]
            self._enqueue(self.entry, args, ())

        while self.worklist:
            key = self.worklist.pop(0)
            self._reanalyze(key)

    def _enqueue(self, fn_name: str, args: List[FrozenSet[Taint]],
                 context: Tuple[str, ...]) -> str:
        arity = len(self.program.functions[fn_name].params)
        shape = _shape_of(args, arity)
        key = f"{fn_name}[{_shape_str(shape)}]|{_ctx_str(context)}"
        if key not in self.summaries:
            self.summaries[key] = Summary(key, fn_name, shape, context)
            self.worklist.append(key)
            self.stats.summaries += 1
        return key

    def _reanalyze(self, key: str) -> None:
        summ = self.summaries[key]
        fn = self.program.functions[summ.fn_name]
        old_ret_origins = frozenset(t.origin for t in summ.returns)
        old_alert_keys = frozenset((a[0].sink_label, a[0].sink_index, a[0].origin)
                                   for a in summ.alerts)

        # 构造参数污点：每个形参一个“代表性 Taint”，trace 只含 ParFrag 前缀。
        # 注意：摘要存的是符号轨迹，param 的绑定在过程内分析入口统一添加。
        param_taints: List[FrozenSet[Taint]] = []
        for i, origins in enumerate(summ.shape):
            ts = set()
            for origin in origins:
                bind = Step("param_bind", "实参绑定到形参", 0, 0, fn.name)
                ts.add(Taint(origin, (ParFrag(i, origin, bind),)))
            param_taints.append(frozenset(ts))

        resolver = _Resolver(self, key)
        result = analyze_intraprocedural(
            fn, param_taints, summ.context, self.cfg, resolver
        )
        summ.returns = result.returns
        summ.alerts = result.alerts
        summ.iterations = result.iterations
        summ.ret_must = result.ret_must
        self.stats.intra_iterations += result.iterations

        new_ret = frozenset(t.origin for t in result.returns)
        new_alerts = frozenset((a[0].sink_label, a[0].sink_index, a[0].origin)
                               for a in result.alerts)
        # 单调格上“增长”= new 中出现了 old 没有的元素（old ⊆ new 应恒成立）。
        # 注意不能只写 old.issubset(new)——空 old 对任何 new 都为 True，会漏掉
        # “从无到有”的增长。
        grew = (not summ.initialized) or (
            not new_ret.issubset(old_ret_origins)
        ) or (not new_alerts.issubset(old_alert_keys))
        summ.initialized = True
        if grew:
            for dep in self.dependents.get(key, set()):
                if dep not in self.worklist:
                    self.worklist.append(dep)
            self.stats.reanalyses += 1

    # ---------------- 调用解析 ----------------
    def resolve_call(
        self,
        caller_key: str,
        caller_fn: str,
        call_index: int,
        callee: str,
        arg_taints: List[FrozenSet[Taint]],
        context: Tuple[str, ...],
    ) -> CallResult:
        site_line = self._callsite_line(caller_fn, callee, call_index)
        site_id = f"{caller_fn}:{site_line}#{call_index}"
        new_ctx = _push_ctx(context, site_id, self.cfg.context_k)
        self.stats.max_call_depth = max(
            self.stats.max_call_depth, len(context) + 1
        )

        if callee not in self.program.functions:
            # 未定义函数：保守 —— 返回值可能携带任一污点实参。轨迹上的
            # unknown_propagation 标记由过程内 _transfer 在调用点现场补（那里
            # 有源码位置），见 taint_model 的 call 转移。
            self.unknown_callsites.add((caller_fn, callee, call_index))
            merged: Dict[Origin, Taint] = {}
            for ats in arg_taints:
                for t in ats:
                    old = merged.get(t.origin)
                    if old is None or len(t.trace) < len(old.trace):
                        merged[t.origin] = t
            return CallResult(frozenset(merged.values()), [],
                              reused=False, is_unknown=True)

        arity = len(self.program.functions[callee].params)
        shape = _shape_of(arg_taints, arity)
        callee_key = f"{callee}[{_shape_str(shape)}]|{_ctx_str(new_ctx)}"

        # 依赖（可能的自递归也记录，无害）
        self.dependents.setdefault(callee_key, set()).add(caller_key)

        if callee_key not in self.summaries:
            # 首遇：以空摘要占位并入队（递归调用会先读到空摘要，后续单调增长）。
            # 依赖边必须在此时就登记，否则 callee 增长后无法触发调用者重算。
            self.summaries[callee_key] = Summary(
                callee_key, callee, shape, new_ctx
            )
            self.stats.summaries += 1
            self.dependents.setdefault(callee_key, set()).add(caller_key)
            self.worklist.append(callee_key)
            return CallResult(frozenset(), [], reused=False, is_unknown=False)

        summ = self.summaries[callee_key]
        self.dependents.setdefault(callee_key, set()).add(caller_key)

        # 摘要轨迹是纯符号的（以 SrcFrag 或 ParFrag(arg_index) 开头）。
        # 不在此处归约——β-归约需要 caller 现场的实参轨迹，由 taint_model 的
        # _transfer 在处理 call 指令时用 beta_reduce 完成（支持任意嵌套）。
        return CallResult(frozenset(summ.returns), list(summ.alerts),
                          reused=True, is_unknown=False)

    def must_returns(
        self,
        caller_fn: str,
        call_index: int,
        callee: str,
        arg_must: List[FrozenSet[Origin]],
        context: Tuple[str, ...],
    ) -> FrozenSet[Origin]:
        """may/must 确认遍的跨函数查询。

        被调摘要的 ``ret_must`` 以其**形参 origin**（即调用者实参的 origin）
        或内部 source origin 表示。这里逐形参核对：只有被调函数“必然返回”
        的形参 origin 同时出现在本次实参的 must 集中，才在调用点成立；内部
        source 直接成立。

        未知函数 / 尚无摘要：保守返回空（标注降级，不影响 may 的漏报安全）。
        """
        if callee not in self.program.functions:
            return frozenset()
        arity = len(self.program.functions[callee].params)
        site_line = self._callsite_line(caller_fn, callee, call_index)
        site_id = f"{caller_fn}:{site_line}#{call_index}"
        new_ctx = _push_ctx(context, site_id, self.cfg.context_k)

        shape = tuple(
            frozenset(arg_must[i]) if i < len(arg_must) else frozenset()
            for i in range(arity)
        )
        callee_key = f"{callee}[{_shape_str(shape)}]|{_ctx_str(new_ctx)}"
        summ = self.summaries.get(callee_key)
        if summ is None or not summ.initialized:
            return frozenset()

        param_origins = set()
        for piece in shape:
            param_origins |= set(piece)

        out: set = set()
        for origin in summ.ret_must:
            if origin in param_origins:
                for must in arg_must:
                    if origin in must:
                        out.add(origin)
                        break
            else:
                # 被调函数内部的 source origin
                out.add(origin)
        return frozenset(out)

    # ---------------- 结果收集 ----------------
    def collect_alerts(self) -> List[Alert]:
        # 以 (sink 所在函数, sink 指令位置, 最外层 origin) 去重，保留最短轨迹
        # 与“最强”的分类标记（definite > 其它过近似原因）。
        # 只收集已能落到具体 source 的告警；trace 仍以 ParFrag 开头的情形是
        # “以空实参播种、从未被带污点调用”的摘要，其根 source 不可解析，跳过。
        rank = {
            "definite": 3,
            "branch_overapprox": 1,
            "recursion_overapprox": 1,
        }
        best: Dict[Tuple[str, str, int, Origin], Alert] = {}
        for summ in self.summaries.values():
            for key, sym_trace, sink_name, ctx, tag in summ.alerts:
                if not sym_trace or isinstance(sym_trace[0], ParFrag):
                    continue
                root = self._root_of(sym_trace)
                fn = self.program.functions[summ.fn_name]
                bb = fn.blocks[key.sink_label]
                ins = bb.instructions[key.sink_index]
                steps = concretize(sym_trace)
                dedup = (summ.fn_name, key.sink_label, key.sink_index, root)
                alert = Alert(
                    origin=root,
                    trace=steps,
                    sink_fn=summ.fn_name,
                    sink_line=ins.span.start.line,
                    sink_col=ins.span.start.col,
                    sink_name=sink_name,
                    context=ctx,
                    fp_tag=tag,
                )
                alert = self._annotate(alert, tag)
                old = best.get(dedup)
                if (old is None
                        or rank[tag] > rank[old.fp_tag]
                        or (rank[tag] == rank[old.fp_tag]
                            and len(alert.trace) < len(old.trace))):
                    best[dedup] = alert
        return sorted(best.values(),
                      key=lambda a: (a.sink_line, a.sink_col, str(a.origin)))

    def _root_of(self, sym_trace: List[Fragment]) -> Origin:
        for f in sym_trace:
            if isinstance(f, SrcFrag):
                return f.origin
        return ("<unknown>", "", -1)  # pragma: no cover

    def _annotate(self, alert: Alert, tag: str) -> Alert:
        """按确认遍标记与轨迹内容给出分类。"""
        if tag == "branch_overapprox":
            alert.may_be_false_positive = True
            alert.classification = "possible_false_positive_branch"
            alert.classification_note = (
                "污点只在部分分支路径存活（典型：一个分支清洗、另一个未清洗）；"
                "分析不对分支条件做常量传播/约束求解，按两支都可能执行过近似"
            )
        kinds = {s.kind for s in alert.trace}
        if "unknown_propagation" in kinds:
            alert.may_be_false_positive = True
            if alert.classification == "true_positive":
                alert.classification = "possible_false_positive"
            alert.classification_note = (
                "路径经过未定义函数，其返回值按可能携带任一污点实参保守处理"
            )
        return alert

    # ---------------- 杂项 ----------------
    def _callsite_line(self, caller_fn: str, callee: str, call_index: int) -> int:
        """根据调用次序找到第 call_index 个指向 callee 的调用指令行号。"""
        fn = self.program.functions.get(caller_fn)
        if fn is None:
            return 0
        seen = 0
        for label in fn.order:
            for ins in fn.blocks[label].instructions:
                if ins.op == "call" and ins.call_name == callee:
                    seen += 1
                    if seen == call_index:
                        return ins.span.start.line
        return 0
