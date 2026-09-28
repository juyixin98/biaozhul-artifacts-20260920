"""检查点执行引擎。

引擎把 :class:`~recomp_scheduler.planner.Plan` 真正跑起来，张量生命周期与
:mod:`recomp_scheduler.memory` 静态模拟器的事件序列**逐条对应**（运行时高水位
必须等于静态预测峰值，否则按 compute_failure 上报实现对账失败）。

前向
  逐块执行。检查点块在入口保存 RNG 快照（计入预算），块结束时释放内部节点的
  激活与 saved 缓冲；**块末（边界）节点的激活与其自身 saved 缓冲保留**——
  激活是后续块的唯一入口，边界节点自己的反向仍需其 saved。若边界被多个后续
  块消费，则每个后续块持有一个 ``retain``：共享子图引用计数由此生效，
  最后一个消费块拆帧后、边界所属块自身拆帧时才真正释放。

反向
  逆序逐块。检查点块先恢复入口 RNG 快照，只重放块内节点（``s..e-1``），
  以 ``mode="recompute"`` 运行——dropout 掩码按键逐字节校验、绝不重复发放
  外部副作用；边界节点 ``e`` 的激活/saved 直接使用前向保留槽。随后块内逆序
  反传，跨块梯度贡献累加到同一梯度槽，再拆帧释放重放帧/边界帧与各持有引用。

预算
  :class:`~recomp_scheduler.tensor.Arena` 持有运行时预算；任何分配或工作区
  进入使高水位超出预算，立即抛 :class:`BudgetInfeasibleError`
  （resource_exhausted，phase="runtime"），与静态不可行（phase="planning"）
  同类别、可凭 details 区分。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .errors import ComputeFailureError, StateConflictError
from .graph import Graph
from .memory import SNAPSHOT_ELEMENTS
from .ops import EffectLog, OpContext, OpEnv, finite_guard, get_spec
from .planner import Plan
from .state import TrainState
from .tensor import Arena


@dataclass
class RunResult:
    outputs: dict[str, np.ndarray]
    input_grads: dict[str, np.ndarray]
    param_grads: dict[str, dict[str, np.ndarray]]
    predicted_peak: int
    runtime_peak: int
    runtime_peak_live_only: int
    plan: Plan
    effects_emitted: int
    effects_replay_verified: int
    trace: list[dict[str, Any]] = field(repr=False)
    block_events: list[dict[str, Any]] = field(repr=False)
    rng_snapshot_balance: dict[str, int] = field(repr=False)

    def peak_match(self) -> bool:
        return self.predicted_peak == self.runtime_peak

    def grad_norms(self) -> dict[str, float]:
        norms: dict[str, float] = {}
        for nid, bucket in self.param_grads.items():
            for pname, g in bucket.items():
                norms[f"{nid}.{pname}"] = float(np.linalg.norm(g))
        for nid, g in self.input_grads.items():
            norms[nid] = float(np.linalg.norm(g))
        return norms


def _allocate_backward_outputs(
    v: str, node: Any, results: list[np.ndarray], arena: Arena,
    grads: dict[str, Any], graph: Graph,
) -> None:
    """按静态模拟器的事件顺序登记反向产物：先参数梯度，后输入梯度槽。

    - 图输入/中间节点的梯度首次出现时分配 ``gacc:<u>``；
    - 图输出节点复用反向开始时注入的 ``gout:<u>`` 槽，不重复计数；
    - 同一输入收到多个消费者的贡献时原地累加（共享子图梯度汇聚）。
    """
    input_grad_arrays = results[: len(node.inputs)]
    param_grad_arrays = results[len(node.inputs) :]

    for spec_p, g_arr in zip(node.params, param_grad_arrays):
        arena.allocate(f"pgrad:{v}:{spec_p.name}", g_arr, kind="gradient")

    for u, g_arr in zip(node.inputs, input_grad_arrays):
        finite_guard(np.asarray(g_arr), v, f"backward-grad->:{u}")
        if u in grads:
            grads[u].data += g_arr
        elif u in graph.outputs:
            grads[u] = arena._live[f"gout:{u}"]
            grads[u].data += g_arr
        else:
            grads[u] = arena.allocate(f"gacc:{u}", g_arr, kind="gradient")


def execute(
    state: TrainState,
    plan: Plan,
    *,
    predicted_peak: int | None = None,
    budget_elements: int | None = None,
    faults: frozenset[str] = frozenset(),
) -> RunResult:
    """按方案执行一次训练步。

    Parameters
    ----------
    state:
        已校验的训练状态（夹具/参数/上游梯度/RNG）。
    plan:
        已通过 :meth:`Plan.validate` 的检查点方案。
    predicted_peak:
        planner 静态预测峰值；与运行时高水位并列返回并强校验一致。
    budget_elements:
        运行时内存预算（元素数）；超出立即抛 resource_exhausted。
    faults:
        测试钩子：集合中节点的**前向**算子被强制失败（compute_failure）；
        重放阶段不注入故障。
    """
    plan.validate(state.graph)
    graph = state.graph
    order = graph.order
    pos = {v: i for i, v in enumerate(order)}
    spans = plan.blocks
    last_block = len(spans) - 1
    effects = EffectLog()
    arena = Arena(budget_elements=budget_elements)
    trace: list[dict[str, Any]] = []
    block_events: list[dict[str, Any]] = []

    def log(event: str, **fields: Any) -> None:
        trace.append(
            {
                "event": event,
                "live": arena.live_elements,
                "peak": arena.peak_elements,
                **fields,
            }
        )

    # ---- 全程驻留：输入与参数 ----
    feeds: dict[str, Any] = {}
    for inp_id in graph.inputs:
        feeds[inp_id] = arena.allocate(
            f"feed:{inp_id}", state.inputs[inp_id], kind="input"
        )
    params_t: dict[str, dict[str, Any]] = {}
    for nid, node in graph.nodes.items():
        if node.params:
            params_t[nid] = {
                spec.name: arena.allocate(
                    f"param:{nid}:{spec.name}",
                    state.params[nid][spec.name],
                    kind="param",
                )
                for spec in node.params
            }

    acts: dict[str, Any] = {}
    saved: dict[str, list[Any]] = {}
    snapshots: dict[int, bytes] = {}
    snapshot_tensors: dict[int, Any] = {}
    grads: dict[str, Any] = {}
    output_values: dict[str, np.ndarray] = {}
    # held_by_block[b]：第 b 块反向拆帧时需要释放的、来自更早块的边界引用
    held_by_block: dict[int, list[str]] = {b: [] for b in range(len(spans))}

    for inp_id in graph.inputs:
        acts[inp_id] = feeds[inp_id]

    def op_env(mode: str) -> OpEnv:
        return OpEnv(rng=state.rng, mode=mode, effects=effects)

    def run_forward_node(v: str, env: OpEnv, *, name_prefix: str = "") -> Any:
        node = graph.node(v)
        spec = get_spec(node.op)
        input_ts = [acts[u] for u in node.inputs]
        for t in input_ts:
            t.require_live()
        ctx = OpContext(
            node_id=v,
            attrs=node.attrs,
            env=env,
            saved=[],
            params=[params_t[v][s.name].data for s in node.params]
            if node.params
            else [],
        )
        ws_size = spec.workspace(node.attrs, node.shape)
        if v in faults and env.mode == "forward":
            raise ComputeFailureError("injected forward fault", node=v)
        try:
            if ws_size:
                # 工作区必须覆盖输出/saved 的分配时刻（与静态模拟器一致）。
                with arena.enter_workspace(ws_size, f"ws_fwd:{v}"):
                    ctx.workspace = np.zeros(ws_size, dtype=np.float64)
                    out_arr = spec.forward_fn(ctx, [t.data for t in input_ts])
                    finite_guard(out_arr, v, f"forward:{env.mode}")
                    prefix = f"{name_prefix}:" if name_prefix else ""
                    out_t = arena.allocate(
                        f"{prefix}act:{v}", out_arr, kind="activation"
                    )
                    saved_ts = [
                        arena.allocate(
                            f"{prefix}saved:{v}:{i}", arr, kind="activation"
                        )
                        for i, arr in enumerate(ctx.saved)
                    ]
            else:
                out_arr = spec.forward_fn(ctx, [t.data for t in input_ts])
                finite_guard(out_arr, v, f"forward:{env.mode}")
                prefix = f"{name_prefix}:" if name_prefix else ""
                out_t = arena.allocate(
                    f"{prefix}act:{v}", out_arr, kind="activation"
                )
                saved_ts = [
                    arena.allocate(f"{prefix}saved:{v}:{i}", arr, kind="activation")
                    for i, arr in enumerate(ctx.saved)
                ]
        except ComputeFailureError:
            raise
        except (FloatingPointError, ValueError, ArithmeticError) as exc:
            raise ComputeFailureError(
                "op forward failed", node=v, mode=env.mode, error=repr(exc)
            ) from exc
        acts[v] = out_t
        saved[v] = saved_ts
        log("forward_node", node=v, mode=env.mode, refs=out_t.ref_count)
        return out_t

    # ================= 前向逐块 =================
    state.mark_forward_done()
    for b, (s, e) in enumerate(spans):
        checkpointed = b < last_block
        if checkpointed:
            snapshots[b] = state.rng_snapshot(b)
            snapshot_tensors[b] = arena.allocate(
                f"snapshot:{b}", np.zeros(SNAPSHOT_ELEMENTS), kind="param"
            )
        env = op_env("forward")
        for i in range(s, e + 1):
            v = order[i]
            if graph.node(v).op == "input":
                continue
            run_forward_node(v, env)
            if v in graph.outputs:
                output_values[v] = acts[v].data.copy()

        if checkpointed:
            boundary = order[e]
            # 仅对非输入边界做引用计数（输入夹具全程驻留，无 act 槽）。
            if graph.node(boundary).op != "input":
                later_blocks = {
                    plan.block_index_of(pos[c])
                    for c in graph.consumers.get(boundary, ())
                    if plan.block_index_of(pos[c]) > b
                }
                for cblock in sorted(later_blocks):
                    arena.retain(acts[boundary], holder=f"block-{cblock}")
                    held_by_block[cblock].append(boundary)
                block_events.append(
                    {
                        "block": b,
                        "phase": "forward_exit",
                        "boundary": boundary,
                        "boundary_refs": acts[boundary].ref_count,
                        "held_for_blocks": sorted(later_blocks),
                    }
                )
            # 释放内部节点（s..e-1）的 saved 与激活；边界帧保留。
            for i in range(s, e):
                v = order[i]
                if graph.node(v).op == "input":
                    continue
                for st in saved.pop(v):
                    arena.release(st)
                arena.release(acts.pop(v))
        else:
            block_events.append(
                {"block": b, "phase": "forward_kept", "checkpointed": False}
            )

    # ================= 前向/反向接缝：注入上游梯度 =================
    gout_t: dict[str, Any] = {}
    for oid in graph.outputs:
        gout_t[oid] = arena.allocate(
            f"gout:{oid}", state.grad_outputs[oid], kind="gradient"
        )
        grads[oid] = gout_t[oid]

    # ================= 反向逐块（逆序） =================
    for b in range(last_block, -1, -1):
        s, e = spans[b]
        replayed = b < last_block

        if replayed:
            state.restore_rng(snapshots.pop(b), b)
            arena.release(snapshot_tensors.pop(b))
            env = op_env("recompute")
            # 只重放内部节点；边界节点 e 的帧为前向保留。
            for i in range(s, e):
                v = order[i]
                if graph.node(v).op == "input":
                    continue
                run_forward_node(v, env, name_prefix="r")
            block_events.append(
                {
                    "block": b,
                    "phase": "replay",
                    "rng_restored": True,
                    "replayed_nodes": [
                        order[i]
                        for i in range(s, e)
                        if graph.node(order[i]).op != "input"
                    ],
                }
            )

        env_bw = OpEnv(rng=state.rng, mode="backward", effects=effects)
        for i in range(e, s - 1, -1):
            v = order[i]
            node = graph.node(v)
            if node.op == "input":
                continue
            spec = get_spec(node.op)
            if v not in grads:
                raise ComputeFailureError(
                    "missing gradient at backward (a required activation path was lost)",
                    node=v,
                    block=b,
                )
            g_out = grads[v]
            input_ts = [acts[u] for u in node.inputs]
            ctx = OpContext(
                node_id=v,
                attrs=node.attrs,
                env=env_bw,
                saved=[t.data for t in saved[v]],
                params=[params_t[v][p.name].data for p in node.params]
                if node.params
                else [],
            )
            wb = spec.backward_workspace(node.attrs, node.shape)
            try:
                if wb:
                    # 工作区覆盖参数梯度/输入梯度槽的分配时刻（顺序同模拟器）。
                    with arena.enter_workspace(wb, f"ws_bwd:{v}"):
                        ctx.workspace = np.zeros(wb, dtype=np.float64)
                        results = spec.backward_fn(
                            ctx,
                            g_out.data,
                            [t.data for t in input_ts],
                            acts[v].data,
                            [t.data for t in saved[v]],
                        )
                        _allocate_backward_outputs(
                            v, node, results, arena, grads, graph
                        )
                else:
                    results = spec.backward_fn(
                        ctx,
                        g_out.data,
                        [t.data for t in input_ts],
                        acts[v].data,
                        [t.data for t in saved[v]],
                    )
                    _allocate_backward_outputs(
                        v, node, results, arena, grads, graph
                    )
            except ComputeFailureError:
                raise
            except (FloatingPointError, ValueError, ArithmeticError) as exc:
                raise ComputeFailureError(
                    "op backward failed", node=v, error=repr(exc)
                ) from exc
            log("backward_node", node=v, block=b)

            # 反传结束立即释放本节点帧：输出梯度槽、saved、激活。
            # （与静态模拟器的节点级生命周期逐条对应。）
            gt = grads.pop(v, None)
            if gt is not None:
                arena.release(gt)
            for st in saved.pop(v, []):
                arena.release(st)
            arena.release(acts.pop(v))

        # 释放本块（作为消费方）持有的更早块边界引用；
        # 边界自身的激活在其所属块的节点反传后才释放。
        for boundary in held_by_block.get(b, []):
            refs_after = acts[boundary].ref_count
            arena.release(acts[boundary])
            block_events.append(
                {
                    "block": b,
                    "phase": "release_boundary_ref",
                    "boundary": boundary,
                    "boundary_refs_after": refs_after - 1,
                }
            )
        block_events.append(
            {"block": b, "phase": "backward_exit", "replayed": replayed}
        )

    # ================= 收尾 =================
    input_grads: dict[str, np.ndarray] = {}
    for v in graph.inputs:
        if v not in grads:
            raise ComputeFailureError("no gradient produced for graph input", node=v)
        gt = grads.pop(v)
        input_grads[v] = gt.data.copy()
        arena.release(gt)

    param_grads: dict[str, dict[str, np.ndarray]] = {}
    for nid, node in graph.nodes.items():
        bucket = {}
        for spec_p in node.params:
            t = arena._live.get(f"pgrad:{nid}:{spec_p.name}")
            if t is None:
                raise ComputeFailureError(
                    "missing parameter gradient", node=nid, param=spec_p.name
                )
            bucket[spec_p.name] = t.data.copy()
        if bucket:
            param_grads[nid] = bucket

    balance = state.snapshot_balance()
    if balance["taken"] != balance["restored"]:
        raise StateConflictError("RNG snapshot balance not zero at run end", **balance)
    # 副作用对账：恰好"检查点块内部随机节点"被重放校验；
    # 边界随机节点与最后一块随机节点不重放（saved 掩码前向保留）。
    replayed_random_nodes = {
        order[i]
        for b in range(last_block)
        for i in range(spans[b][0], spans[b][1])
        if graph.node(order[i]).is_random
    }
    all_random_nodes = {v for v in order if graph.node(v).is_random}
    if effects.replay_verified != len(replayed_random_nodes):
        raise StateConflictError(
            "replayed RNG effect count mismatch",
            expected=len(replayed_random_nodes),
            verified=effects.replay_verified,
            expected_nodes=sorted(replayed_random_nodes),
        )
    unverified = set(effects.unverified())
    expected_unverified = all_random_nodes - replayed_random_nodes
    if unverified != expected_unverified:
        raise StateConflictError(
            "unverified RNG effects differ from expectation",
            unverified=sorted(unverified),
            expected=sorted(expected_unverified),
        )
    if len(effects.emitted) != len(all_random_nodes):
        raise StateConflictError(
            "forward emitted an unexpected number of effects",
            emitted=len(effects.emitted),
            random_nodes=sorted(all_random_nodes),
        )
    if grads:
        raise StateConflictError(
            "stray gradient slots at run end", slots=sorted(grads)
        )

    # 卸载参数、输入、参数梯度（结果已拷出）。
    for nid in list(params_t):
        for t in params_t[nid].values():
            arena.release(t)
    for inp_id in graph.inputs:
        arena.release(feeds[inp_id])
    for nid, node in graph.nodes.items():
        for spec_p in node.params:
            t = arena._live.get(f"pgrad:{nid}:{spec_p.name}")
            if t is not None:
                arena.release(t)

    ledger = arena.close()
    runtime_peak = ledger["peak_elements"]
    if predicted_peak is not None and runtime_peak != predicted_peak:
        raise ComputeFailureError(
            "runtime peak memory disagrees with planner prediction",
            predicted=int(predicted_peak),
            runtime=int(runtime_peak),
            runtime_live_only=ledger["peak_live_only"],
        )

    return RunResult(
        outputs=output_values,
        input_grads=input_grads,
        param_grads=param_grads,
        predicted_peak=int(predicted_peak)
        if predicted_peak is not None
        else runtime_peak,
        runtime_peak=runtime_peak,
        runtime_peak_live_only=ledger["peak_live_only"],
        plan=plan,
        effects_emitted=len(effects.emitted),
        effects_replay_verified=effects.replay_verified,
        trace=trace,
        block_events=block_events,
        rng_snapshot_balance=balance,
    )
