"""Z3 有界模型检查 (Bounded Model Checking) 核心。

编码：
- int 变量/参数 -> 64 位位向量 (BitVec64)，算术自动按补码回绕，比较为有符号；
- bool 变量 -> Bool；
- 每一步恰好选择一个动作，未选中动作的守卫不约束；
- 所有 effect 右端在前态上同时求值（frame 公理：未被赋值的变量保持不变）；
- 性质在每一步（含初始状态）都必须成立；

通过迭代加深 (0..bound) 找首个可满足的违例深度，即最短反例。
所有结论严格限定在给定步数内，绝不断言任意深度安全。
"""

from __future__ import annotations

import time
from typing import Any

import z3

from .errors import ModelError
from .invariants import default_invariant
from .replay import verify_trace

MAX_BOUND = 40
MAX_TIMEOUT_MS = 600_000
MIN_TIMEOUT_MS = 50


def _z3_expr(expr: dict, env: dict[str, Any]) -> z3.ExprRef:
    t = expr["t"]
    if t == "num":
        return z3.BitVecVal(expr["value"] & ((1 << 64) - 1), 64)
    if t == "bool":
        return z3.BoolVal(bool(expr["value"]))
    if t == "var":
        return env[expr["name"]]
    if t == "arith":
        vals = [_z3_expr(a, env) for a in expr["args"]]
        r = vals[0]
        for v in vals[1:]:
            if expr["op"] == "+":
                r = r + v
            elif expr["op"] == "-":
                r = r - v
            else:
                r = r * v
        return r
    if t == "cmp":
        l = _z3_expr(expr["lhs"], env)
        r = _z3_expr(expr["rhs"], env)
        op = expr["op"]
        if op == "<":
            return l < r
        if op == "<=":
            return l <= r
        if op == ">":
            return l > r
        if op == ">=":
            return l >= r
        if op == "==":
            return l == r
        return l != r
    if t == "boolop":
        vals = [_z3_expr(a, env) for a in expr["args"]]
        op = expr["op"]
        if op == "and":
            return z3.And(vals) if vals else z3.BoolVal(True)
        if op == "or":
            return z3.Or(vals) if vals else z3.BoolVal(False)
        return z3.Not(vals[0])
    raise ModelError(f"编码时遇到未知表达式类型 {t!r}")


def _as_signed(v: Any) -> int:
    # as_signed_long() 对 BitVecNum 返回 64 位有符号补码解释的整数
    return z3.simplify(v).as_signed_long()


class BMC:
    def __init__(self, model: dict):
        self.model = model
        self.int_vars = [v["name"] for v in model["state_vars"] if v["type"] == "int"]
        self.bool_vars = [v["name"] for v in model["state_vars"] if v["type"] == "bool"]
        inv = model.get("invariant") or default_invariant(model)
        self.inv_expr = inv["expr"]

    # -- 单步编码 -----------------------------------------------------------

    def _fresh_state(self, k: int) -> dict[str, z3.ExprRef]:
        st: dict[str, z3.ExprRef] = {}
        for n in self.int_vars:
            st[n] = z3.BitVec(f"s{k}_{n}", 64)
        for n in self.bool_vars:
            st[n] = z3.Bool(f"s{k}_{n}")
        return st

    def _initial_constraints(self, s: dict[str, z3.ExprRef]) -> list[z3.BoolRef]:
        c = []
        for v in self.model["state_vars"]:
            n = v["name"]
            if v["type"] == "int":
                c.append(s[n] == z3.BitVecVal(v["init"] & ((1 << 64) - 1), 64))
            else:
                c.append(s[n] == z3.BoolVal(bool(v["init"])))
        return c

    def _encode_step(self, cur: dict[str, z3.ExprRef], nxt: dict[str, z3.ExprRef],
                     k: int) -> tuple[list[z3.BoolRef], list[z3.BoolRef]]:
        """返回 (转移约束, 动作选择变量)。"""
        cons: list[z3.BoolRef] = []
        taken: list[z3.BoolRef] = []
        # 每个动作对下一状态给出的候选值
        candidate_int: dict[str, list[z3.ExprRef]] = {n: [] for n in self.int_vars}
        candidate_bool: dict[str, list[z3.ExprRef]] = {n: [] for n in self.bool_vars}

        for ai, act in enumerate(self.model["actions"]):
            sel = z3.Bool(f"step{k}_act{ai}_{act['name']}")
            taken.append(sel)

            params = {p["name"]: z3.BitVec(f"step{k}_{act['name']}_{p['name']}", 64)
                      for p in act["params"]}
            env = dict(cur)
            env.update(params)

            for p in act["params"]:
                pv = params[p["name"]]
                cons.append(z3.Implies(
                    sel,
                    z3.And(pv >= z3.BitVecVal(p["min"], 64),
                           pv <= z3.BitVecVal(p["max"], 64)),
                ))
            for g in act["guards"]:
                cons.append(z3.Implies(sel, _z3_expr(g["expr"], env)))

            assigned: dict[str, z3.ExprRef] = {}
            for e in act["effects"]:
                if e["kind"] == "assign":
                    assigned[e["target"]] = _z3_expr(e["expr"], env)
                elif e["kind"] == "lock":
                    assigned[e["target"]] = z3.BoolVal(True)
                else:
                    assigned[e["target"]] = z3.BoolVal(False)

            for n in self.int_vars:
                candidate_int[n].append(assigned.get(n, cur[n]))
            for n in self.bool_vars:
                candidate_bool[n].append(assigned.get(n, cur[n]))

        # 恰好一个动作
        cons.append(z3.PbEq([(t, 1) for t in taken], 1))

        def chain_ite(sel_list: list[z3.BoolRef], vals: list[z3.ExprRef]) -> z3.ExprRef:
            """嵌套 ITE：按 sel0 ? v0 : sel1 ? v1 : ... : v_last。"""
            assert len(sel_list) == len(vals) and len(vals) >= 1
            expr = vals[-1]
            for i in range(len(vals) - 2, -1, -1):
                expr = z3.If(sel_list[i], vals[i], expr)
            return expr

        # frame 公理：下一状态 = 选中动作的候选值
        for n in self.int_vars:
            cons.append(nxt[n] == chain_ite(taken, candidate_int[n]))
        for n in self.bool_vars:
            cons.append(nxt[n] == chain_ite(taken, candidate_bool[n]))
        return cons, taken

    # -- 主流程 --------------------------------------------------------------

    def check(self, bound: int, timeout_ms: int) -> dict:
        if not isinstance(bound, int) or isinstance(bound, bool) or not (0 <= bound <= MAX_BOUND):
            raise ModelError(f"steps 必须是 [0,{MAX_BOUND}] 内的整数")
        if not isinstance(timeout_ms, int) or not (MIN_TIMEOUT_MS <= timeout_ms <= MAX_TIMEOUT_MS):
            raise ModelError(f"timeout_ms 必须是 [{MIN_TIMEOUT_MS},{MAX_TIMEOUT_MS}] 内的整数")

        deadline = time.monotonic() + timeout_ms / 1000.0
        stats = {"depths_checked": 0, "solver_calls": 0,
                 "solver_time_ms": 0, "per_depth_ms": []}

        def remaining() -> int:
            return int((deadline - time.monotonic()) * 1000)

        # 增量单 solver：随深度逐步加入转移约束并复用已学到的子句。
        # 第 k 轮查询“初始状态 + 前 k 步转移 + 第 k 步不变量被否定”，
        # 且只有第 0..k-1 步的不变量在查询前已被固化为真。
        # 因此 sat 时深度 k 就是（0..bound 范围内的）最短反例。
        solver = z3.Solver()
        states = [self._fresh_state(0)]
        solver.add(self._initial_constraints(states[0]))

        def query(depth: int) -> tuple:
            """在 depth 深度查询一次，返回 (answer, elapsed_ms)。"""
            budget = remaining()
            if budget < MIN_TIMEOUT_MS:
                return "budget_exhausted", 0
            t0 = time.monotonic()
            try:
                solver.set("timeout", max(budget, MIN_TIMEOUT_MS))
                ans = solver.check()
            except z3.Z3Exception as exc:
                # 求解器内部异常（资源中止等）：归入 unknown 并如实携带原因
                return "z3_error:" + str(exc), int((time.monotonic() - t0) * 1000)
            return ans, int((time.monotonic() - t0) * 1000)

        def record(depth: int, ans, elapsed_ms: int) -> None:
            stats["solver_calls"] += 1
            stats["solver_time_ms"] += elapsed_ms
            stats["depths_checked"] = depth + 1
            stats["per_depth_ms"].append({"depth": depth, "result": str(ans),
                                          "elapsed_ms": elapsed_ms})

        def classify(ans, depth: int, bound: int):
            """把 query() 的回答映射为最终结果；None 表示应继续加深。"""
            if ans == "budget_exhausted":
                return self._result_timeout(depth, bound, "总超时预算已用尽", stats)
            if isinstance(ans, str) and ans.startswith("z3_error:"):
                return {
                    "status": "unknown", "depth": depth, "bound": bound,
                    "trace": None,
                    "reason": f"Z3 在深度 {depth} 抛出异常: {ans[len('z3_error:'):]}",
                    "note": "求解器内部错误，既未发现反例也不能排除。",
                    "stats": stats,
                }
            if ans == z3.sat:
                return "__sat__"
            if ans == z3.unknown:
                return self._unknown_result(solver, depth, bound, stats)
            return None  # unsat -> 继续加深

        # 深度 0：初始状态是否直接破坏不变量
        solver.push()
        solver.add(z3.Not(_z3_expr(self.inv_expr, states[0])))
        ans, ms = query(0)
        if ans == z3.sat:
            trace0 = self._extract_trace(solver, 0)
        solver.pop()
        record(0, ans, ms)
        verdict = classify(ans, 0, bound)
        if verdict == "__sat__":
            return self._cex_result(trace0, 0, bound, stats)
        if verdict is not None:
            return verdict
        # 初始状态性质成立，固化后进入增量加深
        solver.add(_z3_expr(self.inv_expr, states[0]))

        for depth in range(1, bound + 1):
            nxt = self._fresh_state(depth)
            cons, _ = self._encode_step(states[depth - 1], nxt, depth - 1)
            solver.add(cons)
            states.append(nxt)

            solver.push()
            solver.add(z3.Not(_z3_expr(self.inv_expr, nxt)))
            ans, ms = query(depth)
            if ans == z3.sat:
                trace_d = self._extract_trace(solver, depth)
            solver.pop()
            record(depth, ans, ms)

            verdict = classify(ans, depth, bound)
            if verdict == "__sat__":
                return self._cex_result(trace_d, depth, bound, stats)
            if verdict is not None:
                return verdict
            # 该深度无反例：固化性质，继续加深
            solver.add(_z3_expr(self.inv_expr, nxt))

        return {
            "status": "no_counterexample",
            "depth": None,
            "bound": bound,
            "trace": None,
            "reason": f"在 0..{bound} 步的所有可达状态上不变量均成立",
            "note": "这是有界结果：未发现反例不等于证明任意深度安全。",
            "stats": stats,
        }

    def _result_timeout(self, depth: int, bound: int, reason: str, stats: dict) -> dict:
        return {"status": "timeout", "depth": depth, "bound": bound, "trace": None,
                "reason": reason,
                "note": "时间预算内无法完成检查；已排除的深度见 stats。", "stats": stats}

    def _unknown_result(self, solver: z3.Solver, depth: int, bound: int,
                        stats: dict) -> dict:
        reason = str(solver.reason_unknown())
        if "timeout" in reason.lower():
            return self._result_timeout(
                depth, bound, f"Z3 在深度 {depth} 达到时间限制 ({reason})", stats)
        return {
            "status": "unknown",
            "depth": depth,
            "bound": bound,
            "trace": None,
            "reason": f"Z3 在深度 {depth} 返回 unknown: {reason}",
            "note": "求解器未能判定，既未发现反例也不能排除。",
            "stats": stats,
        }

    def _cex_result(self, trace: list[dict], depth: int, bound: int,
                    stats: dict) -> dict:
        # 用具体解释器交叉重放，保证反例真实可复现
        replay = verify_trace(self.model, trace, self.inv_expr)
        return {
            "status": "counterexample",
            "depth": depth,
            "bound": bound,
            "trace": replay["trace"],
            "reason": f"在第 {depth} 步发现不变量被破坏（0..{bound} 范围内最短）",
            "note": "仅覆盖给定步数内的执行，不代表任意深度安全。",
            "stats": stats,
        }

    def _extract_trace(self, solver: z3.Solver, depth: int) -> list[dict]:
        m = solver.model()
        actions = self.model["actions"]

        def state_values(k: int) -> dict[str, Any]:
            vals: dict[str, Any] = {}
            for n in self.int_vars:
                sym = z3.BitVec(f"s{k}_{n}", 64)
                vals[n] = _as_signed(m.eval(sym, model_completion=True))
            for n in self.bool_vars:
                sym = z3.Bool(f"s{k}_{n}")
                vals[n] = bool(z3.is_true(m.eval(sym, model_completion=True)))
            return vals

        trace: list[dict] = [{"step": 0, "action": None, "params": {},
                              "state": state_values(0)}]
        for k in range(depth):
            chosen = None
            chosen_params: dict[str, int] = {}
            for ai, act in enumerate(actions):
                sel = z3.Bool(f"step{k}_act{ai}_{act['name']}")
                if z3.is_true(m.eval(sel, model_completion=True)):
                    chosen = act
                    for p in act["params"]:
                        sym = z3.BitVec(f"step{k}_{act['name']}_{p['name']}", 64)
                        chosen_params[p["name"]] = _as_signed(
                            m.eval(sym, model_completion=True))
                    break
            if chosen is None:
                raise ModelError("内部错误：无法从模型中确定第 %d 步选择的动作" % k)
            trace.append({"step": k + 1, "action": chosen["name"],
                          "params": chosen_params,
                          "state": state_values(k + 1)})
        return trace
