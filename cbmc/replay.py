"""逐步重放求解器反例，并用具体解释器交叉校验。

这是「计算必须真实执行」的关键一环：不信任 Z3 模型直接读出的状态序列，
而是从初始状态开始，用 Python 解释器按动作/参数一步步重新执行，逐字段比对。
任何对不上都抛 ReplayError（视为内部错误，绝不当成有效反例返回）。
"""

from __future__ import annotations

from .errors import ReplayError
from .eval import apply_action, eval_expr, initial_state, wrap64


def replay_trace(model: dict, steps: list[dict], inv_expr: dict) -> list[dict]:
    """给定 [{action, params}]（不含初始步），返回带状态与不变量判定的完整轨迹。"""
    actions = {a["name"]: a for a in model["actions"]}
    state = initial_state(model)
    trace = [{"step": 0, "action": None, "params": {}, "state": state,
              "invariant_holds": bool(eval_expr(inv_expr, state))}]
    for i, st in enumerate(steps, start=1):
        aname = st.get("action")
        if aname not in actions:
            raise ReplayError(f"第 {i} 步动作 {aname!r} 不存在于模型中")
        act = actions[aname]
        params_in = st.get("params", {})
        if not isinstance(params_in, dict):
            raise ReplayError(f"第 {i} 步 params 必须是对象")
        params: dict[str, int] = {}
        for p in act["params"]:
            if p["name"] not in params_in:
                raise ReplayError(f"第 {i} 步缺少参数 {p['name']!r}")
            v = params_in[p["name"]]
            if not isinstance(v, int) or isinstance(v, bool):
                raise ReplayError(f"第 {i} 步参数 {p['name']!r} 必须是整数")
            v = wrap64(v)
            if not (p["min"] <= v <= p["max"]):
                raise ReplayError(
                    f"第 {i} 步参数 {p['name']}={v} 超出声明范围 [{p['min']},{p['max']}]")
            params[p["name"]] = v
        extra = set(params_in) - {p["name"] for p in act["params"]}
        if extra:
            raise ReplayError(f"第 {i} 步出现未声明参数 {sorted(extra)}")

        env = dict(state)
        env.update(params)
        for g in act["guards"]:
            if not bool(eval_expr(g["expr"], env)):
                raise ReplayError(f"第 {i} 步动作 {aname} 的守卫在该状态下不成立")
        state = apply_action(act, state, params)
        trace.append({"step": i, "action": aname, "params": params, "state": state,
                      "invariant_holds": bool(eval_expr(inv_expr, state))})
    return trace


def verify_trace(model: dict, trace: list[dict], inv_expr: dict) -> dict:
    """校验 checker 提取的 trace 是否能被具体解释器如实复现。

    - trace[0] 必须等于声明的初始状态；
    - 每一步的守卫必须成立，重放状态必须与求解器给出的逐字段相等；
    - 最后一个状态必须确实破坏不变量（前面的状态也记录判定）。
    """
    if not trace:
        raise ReplayError("反例轨迹为空")

    init = initial_state(model)
    if trace[0].get("state") != init:
        raise ReplayError(
            f"轨迹初始状态与模型声明不一致: {trace[0].get('state')} != {init}")
    if trace[0].get("action") is not None:
        raise ReplayError("轨迹第 0 步不应包含动作")

    replayed = replay_trace(
        model,
        [{"action": t["action"], "params": t.get("params", {})} for t in trace[1:]],
        inv_expr,
    )

    for given, concrete in zip(trace, replayed):
        if given.get("step") != concrete["step"]:
            raise ReplayError(f"第 {concrete['step']} 步编号不一致")
        if given.get("action") != concrete["action"]:
            raise ReplayError(f"第 {concrete['step']} 步动作不一致")
        if given.get("params", {}) != concrete["params"]:
            raise ReplayError(f"第 {concrete['step']} 步参数不一致")
        if given.get("state") != concrete["state"]:
            raise ReplayError(
                f"第 {concrete['step']} 步状态不一致: 求解器={given.get('state')} "
                f"重放={concrete['state']}")

    if replayed[-1]["invariant_holds"]:
        raise ReplayError("重放结束时不变量仍然成立，反例不成立")

    return {"trace": replayed}
