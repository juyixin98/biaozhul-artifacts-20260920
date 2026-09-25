"""声明式 JSON 请求接口：描述 DAG 与输入，返回规划与验证报告。

请求样例见 ``examples/requests/*.json``。结构：

````
{
  "inputs": {"A": [2, 2], "B": [2, 2]},
  "ops": [
    {"name": "C", "op": "add",    "inputs": ["A", "B"], "time": 1},
    {"name": "D", "op": "matmul", "inputs": ["A", "C"], "time": 2},
    {"name": "v", "op": "slice",  "src": "X",
     "starts": [0, 0], "sizes": [2, 2], "time": 1}
  ],
  "seed": 42,                       # 可选，合成数据种子
  "feeds": {"A": [[..]]},           # 可选，显式给定输入，缺省用合成数据
  "budget_bytes": 1024              # 可选，峰值预算（字节）
}
`````

``time`` 可省略，此时按 ops 的书写顺序从 1 开始自动编号。
没有任何消费者的值自动成为图输出。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

import numpy as np

from .dag import DAG, DAGBuilder, DAGValidationError
from .executor import DTYPE, ITEMSIZE
from .planner import Plan
from .verifier import Verification, verify_plan


class RequestError(ValueError):
    """请求结构不合法。"""


def _resolve_time(raw_op: Mapping[str, Any], auto_time: int) -> tuple[int, int]:
    """解析 op 时刻：缺省按出现顺序自动编号。返回 (time, 新的 auto_time)。"""
    time = raw_op.get("time")
    if time is None:
        return auto_time + 1, auto_time + 1
    return int(time), int(time)


def _apply_op(
    builder: DAGBuilder, raw_op: Mapping[str, Any], name: str, time: int
) -> None:
    """把单个 op 描述写入 builder。"""
    op_type = raw_op.get("op")
    if op_type in ("add", "matmul"):
        inputs = _require_inputs(raw_op, 2, name)
        if op_type == "add":
            builder.add_add(name, inputs[0], inputs[1], time)
        else:
            builder.add_matmul(name, inputs[0], inputs[1], time)
        return
    if op_type == "slice":
        src = raw_op.get("src")
        if not isinstance(src, str):
            raise RequestError(f"slice {name!r} 需要字符串字段 src")
        starts = raw_op.get("starts")
        sizes = raw_op.get("sizes")
        if not isinstance(starts, list) or not isinstance(sizes, list):
            raise RequestError(f"slice {name!r} 需要整数列表 starts 与 sizes")
        builder.add_slice(name, src, starts, sizes, time)
        return
    raise RequestError(
        f"op {name!r} 类型 {op_type!r} 不受支持"
        "（仅支持 add / matmul / slice）"
    )


def build_dag_from_dict(spec: Mapping[str, Any]) -> DAG:
    """把请求字典解析为 :class:`DAG`。"""
    if not isinstance(spec, Mapping):
        raise RequestError("请求顶层必须是 JSON 对象")

    raw_inputs = spec.get("inputs")
    if not isinstance(raw_inputs, Mapping) or not raw_inputs:
        raise RequestError("inputs 必须是非空对象: {名字: [形状...]}")

    builder = DAGBuilder()
    for name, shape in raw_inputs.items():
        if not isinstance(shape, list) or not shape:
            raise RequestError(f"输入 {name!r} 的形状必须是非空整数列表")
        builder.add_input(str(name), shape)

    raw_ops = spec.get("ops", [])
    if not isinstance(raw_ops, list) or not raw_ops:
        raise RequestError("ops 必须是非空列表")

    auto_time = 0
    for index, raw_op in enumerate(raw_ops):
        if not isinstance(raw_op, Mapping):
            raise RequestError(f"第 {index} 个 op 必须是对象")
        name = raw_op.get("name")
        if not name:
            raise RequestError(f"第 {index} 个 op 缺少 name")
        time, auto_time = _resolve_time(raw_op, auto_time)
        try:
            _apply_op(builder, raw_op, str(name), time)
        except DAGValidationError as exc:
            raise RequestError(f"op {name!r} 非法: {exc}") from exc

    return builder.build()


def _require_inputs(raw_op: Mapping[str, Any], count: int, name: str) -> List[str]:
    inputs = raw_op.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != count:
        raise RequestError(
            f"op {name!r} 需要恰好 {count} 个 inputs"
        )
    if not all(isinstance(x, str) for x in inputs):
        raise RequestError(f"op {name!r} 的 inputs 必须是字符串列表")
    return list(inputs)


def make_feeds(
    dag: DAG,
    spec: Mapping[str, Any],
) -> Dict[str, np.ndarray]:
    """从请求构造输入数据：显式 feeds 优先，否则用可复现合成数据。"""
    seed = int(spec.get("seed", 42))
    feeds: Dict[str, np.ndarray] = {}
    explicit = spec.get("feeds")
    if explicit is not None and not isinstance(explicit, Mapping):
        raise RequestError("feeds 必须是对象 {名字: 嵌套数组}")

    for name, shape in dag.inputs.items():
        if explicit is not None and name in explicit:
            arr = np.asarray(explicit[name], dtype=DTYPE)
            if tuple(arr.shape) != tuple(shape):
                raise RequestError(
                    f"feed {name!r} 形状 {arr.shape} 与声明 {shape} 不符"
                )
            feeds[name] = arr.astype(DTYPE, copy=True)
        else:
            # 可复现合成数据：按输入名派生独立子流，取值 [-1, 1)
            sub_seed = (seed + _stable_hash(name)) & 0xFFFFFFFF
            rng = np.random.default_rng(sub_seed)
            feeds[name] = rng.random(size=shape, dtype=DTYPE) * 2.0 - 1.0
    return feeds


def _stable_hash(text: str) -> int:
    """与 Python 哈希随机化无关的稳定字符串散列（FNV-1a 32bit）。"""
    h = 0x811C9DC5
    for ch in text.encode("utf-8"):
        h ^= ch
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def _parse_budget(spec: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """解析字节预算为元素预算。返回 (元素预算或 None, 原始字节预算)。"""
    budget_bytes = spec.get("budget_bytes")
    if budget_bytes is None:
        return None, None
    if not isinstance(budget_bytes, int) or budget_bytes <= 0:
        raise RequestError("budget_bytes 必须是正整数")
    return budget_bytes // ITEMSIZE, budget_bytes


def _placements_report(plan: Plan) -> List[Dict[str, Any]]:
    return [
        {
            "root": slot.root,
            "offset": slot.offset,
            "size": slot.size,
            "members": list(slot.members),
            "alive": [slot.interval.start, slot.interval.end],
        }
        for slot in sorted(plan.placements.values(), key=lambda s: s.offset)
    ]


def _memory_report(
    v: Verification, budget_bytes: int | None
) -> Dict[str, Any]:
    return {
        "dtype": str(DTYPE),
        "itemsize_bytes": ITEMSIZE,
        "arena_elements": v.arena_elements,
        "arena_bytes": v.arena_bytes,
        "peak_live_elements": v.peak_live_elements,
        "naive_no_reuse_peak_elements": v.naive_peak_elements,
        "reuse_peak_elements": v.reuse_peak_elements,
        "naive_total_allocated_elements": v.naive_total_allocated,
        "reuse_total_allocated_elements": v.reuse_total_allocated,
        "saved_total_elements": v.saved_total_elements,
        "saved_total_ratio": round(v.saved_total_ratio, 6),
        "within_budget": v.within_budget,
        "budget_bytes": budget_bytes,
    }


def handle_request(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """处理一个完整请求，返回可 JSON 序列化的报告字典。"""
    dag = build_dag_from_dict(spec)
    feeds = make_feeds(dag, spec)
    budget_elements, budget_bytes = _parse_budget(spec)
    verification = verify_plan(dag, feeds, budget_elements=budget_elements)
    plan = verification.plan

    return {
        "passed": verification.passed,
        "values": dag.values,
        "outputs": verification.output_names,
        "intervals": {
            root: [iv.start, iv.end]
            for root, iv in sorted(plan.storage_intervals.items())
        },
        "placements": _placements_report(plan),
        "reuse_chains": [
            {"offset": offset, "sequence": chain}
            for offset, chain in plan.reuse_chains
        ],
        "memory": _memory_report(verification, budget_bytes),
        "correctness": {
            "max_abs_diff_vs_naive": verification.max_abs_diff,
            "outputs_match": np.isfinite(verification.max_abs_diff)
            and verification.max_abs_diff <= 1e-12,
        },
        "live_elements_per_time": {
            str(t): n for t, n in sorted(plan.live_per_time.items())
        },
        "errors": verification.errors,
    }
