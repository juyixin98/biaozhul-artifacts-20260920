"""显式 JSON 合约模型的加载与校验。

模型 schema（所有整数只支持 Python int 范围内的有界整数语义，见检查器）::

    {
      "name": "...",
      "state_vars": [
        {"name": "alice", "type": "int",  "init": 100},
        {"name": "bob",   "type": "int",  "init": 50},
        {"name": "locked", "type": "bool", "init": false}
      ],
      "actions": [
        {
          "name": "transfer",
          "params": [{"name": "amount", "type": "int", "min": 0, "max": 1000}],
          "guards": [{"expr": "<条件 AST>", "..."}],            # 合取
          "effects": [
            {"kind": "assign", "target": "alice", "expr": "<算术 AST>"}
          ]
        }
      ],
      "invariant": {"type": "property", ...}                    # 可选；不给出则
                                                                 # 用默认不变量
    }

表达式 AST（纯数据，没有任何可执行代码）：
  整数: {"t": "num", "value": 7}
  变量: {"t": "var", "name": "alice"}
  算术: {"t": "arith", "op": "+"|"-"|"*", "args": [e, e, ...]}
  比较: {"t": "cmp", "op": "<"|"<="|">"|">="|"=="|"!=", "lhs": e, "rhs": e}
  布尔: {"t": "boolop", "op": "and"|"or"|"not",
         "args": [...]} (not 恰 1 个参数；and/or 0..n 个，0 个参为 and=true/or=false)
  布尔字面量: {"t": "bool", "value": true}
"""

from __future__ import annotations

from typing import Any

from .errors import ModelError

INT_OPS = {"+", "-", "*"}
CMP_OPS = {"<", "<=", ">", ">=", "==", "!="}
BOOL_OPS = {"and", "or", "not"}
TYPES = {"int", "bool"}
EFFECT_KINDS = {"assign", "lock", "unlock"}
MAX_PARAM_VALUE = 2**63 - 1
MIN_PARAM_VALUE = -(2**63)
MAX_NAME_LEN = 64


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _check_name(name: Any, where: str, existing: set[str] | None = None) -> str:
    if not isinstance(name, str) or not name:
        raise ModelError(f"{where}: 名称必须是非空字符串")
    if len(name) > MAX_NAME_LEN:
        raise ModelError(f"{where}: 名称过长 (>{MAX_NAME_LEN})")
    if not name.replace("_", "").isalnum() or name[0].isdigit():
        raise ModelError(f"{where}: 非法标识符 {name!r}（字母/下划线开头，仅含字母数字下划线）")
    if existing is not None and name in existing:
        raise ModelError(f"{where}: 重复定义 {name!r}")
    return name


def validate_expr(expr: Any, int_vars: set[str], bool_vars: set[str], ctx: str) -> str:
    """递归校验表达式 AST，返回推断类型 'int' | 'bool'。"""
    if not isinstance(expr, dict) or "t" not in expr:
        raise ModelError(f"{ctx}: 表达式必须是带 't' 字段的对象，收到 {expr!r}")
    t = expr["t"]

    if t == "num":
        if not _is_int(expr.get("value")):
            raise ModelError(f"{ctx}: num.value 必须是整数")
        return "int"
    if t == "bool":
        if not isinstance(expr.get("value"), bool):
            raise ModelError(f"{ctx}: bool.value 必须是布尔值")
        return "bool"
    if t == "var":
        name = _check_name(expr.get("name"), f"{ctx}.var")
        if name in int_vars:
            return "int"
        if name in bool_vars:
            return "bool"
        raise ModelError(f"{ctx}: 未知变量 {name!r}")
    if t == "arith":
        op = expr.get("op")
        if op not in INT_OPS:
            raise ModelError(f"{ctx}: 不支持的算术算符 {op!r}（仅 {sorted(INT_OPS)}）")
        args = expr.get("args")
        if not isinstance(args, list) or len(args) < 2:
            raise ModelError(f"{ctx}: arith {op} 至少需要 2 个参数")
        for i, a in enumerate(args):
            if validate_expr(a, int_vars, bool_vars, f"{ctx}.{op}[{i}]") != "int":
                raise ModelError(f"{ctx}.{op}[{i}]: 需要整数表达式")
        return "int"
    if t == "cmp":
        op = expr.get("op")
        if op not in CMP_OPS:
            raise ModelError(f"{ctx}: 不支持的比较算符 {op!r}")
        lt = validate_expr(expr.get("lhs"), int_vars, bool_vars, f"{ctx}.{op}.lhs")
        rt = validate_expr(expr.get("rhs"), int_vars, bool_vars, f"{ctx}.{op}.rhs")
        if lt != rt:
            raise ModelError(f"{ctx}.{op}: 两侧类型不一致 ({lt} vs {rt})")
        if lt == "bool" and op not in ("==", "!="):
            raise ModelError(f"{ctx}.{op}: 布尔值只支持 == / !=")
        return "bool"
    if t == "boolop":
        op = expr.get("op")
        if op not in BOOL_OPS:
            raise ModelError(f"{ctx}: 不支持的布尔算符 {op!r}")
        args = expr.get("args")
        if not isinstance(args, list):
            raise ModelError(f"{ctx}: boolop 需要 args 列表")
        if op == "not":
            if len(args) != 1:
                raise ModelError(f"{ctx}: not 恰需 1 个参数")
        for i, a in enumerate(args):
            if validate_expr(a, int_vars, bool_vars, f"{ctx}.{op}[{i}]") != "bool":
                raise ModelError(f"{ctx}.{op}[{i}]: 需要布尔表达式")
        return "bool"
    raise ModelError(f"{ctx}: 未知表达式类型 {t!r}")


def validate_model(model: Any) -> dict:
    """校验整个合约模型，返回规范化后的纯数据（深拷贝 + 默认值）。"""
    if not isinstance(model, dict):
        raise ModelError("模型必须是 JSON 对象")

    name = model.get("name", "contract")
    if not isinstance(name, str):
        raise ModelError("name 必须是字符串")

    raw_vars = model.get("state_vars")
    if not isinstance(raw_vars, list) or not raw_vars:
        raise ModelError("state_vars 必须是非空数组")

    int_vars: set[str] = set()
    bool_vars: set[str] = set()
    norm_vars: list[dict] = []
    for i, v in enumerate(raw_vars):
        if not isinstance(v, dict):
            raise ModelError(f"state_vars[{i}] 必须是对象")
        vname = _check_name(v.get("name"), f"state_vars[{i}]", int_vars | bool_vars)
        vtype = v.get("type")
        if vtype not in TYPES:
            raise ModelError(f"state_vars[{i}].type 必须是 {sorted(TYPES)}")
        init = v.get("init")
        if vtype == "int":
            if not _is_int(init):
                raise ModelError(f"state_vars[{i}].init 必须是整数")
            int_vars.add(vname)
        else:
            if not isinstance(init, bool):
                raise ModelError(f"state_vars[{i}].init 必须是布尔值")
            bool_vars.add(vname)
        norm_vars.append({"name": vname, "type": vtype, "init": init})

    raw_actions = model.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ModelError("actions 必须是非空数组")

    action_names: set[str] = set()
    norm_actions: list[dict] = []
    for i, act in enumerate(raw_actions):
        if not isinstance(act, dict):
            raise ModelError(f"actions[{i}] 必须是对象")
        aname = _check_name(act.get("name"), f"actions[{i}]", action_names)
        action_names.add(aname)

        params = act.get("params", [])
        if not isinstance(params, list):
            raise ModelError(f"actions[{i}].params 必须是数组")
        param_names: set[str] = set()
        norm_params: list[dict] = []
        for j, p in enumerate(params):
            if not isinstance(p, dict) or p.get("type") != "int":
                raise ModelError(f"actions[{i}].params[{j}]: 目前仅支持 int 参数")
            pname = _check_name(p.get("name"), f"actions[{i}].params[{j}]", param_names)
            param_names.add(pname)
            lo, hi = p.get("min", 0), p.get("max", 0)
            if not (_is_int(lo) and _is_int(hi)) or lo > hi:
                raise ModelError(
                    f"actions[{i}].params[{j}]: 需要 min <= max 的整数边界"
                )
            if lo < MIN_PARAM_VALUE or hi > MAX_PARAM_VALUE:
                raise ModelError(
                    f"actions[{i}].params[{j}]: 参数边界超出 63 位有符号整数范围"
                )
            norm_params.append({"name": pname, "type": "int", "min": lo, "max": hi})

        scope_int = int_vars | param_names
        guards = act.get("guards", [])
        if not isinstance(guards, list):
            raise ModelError(f"actions[{i}].guards 必须是数组")
        norm_guards: list[dict] = []
        for j, g in enumerate(guards):
            if not isinstance(g, dict) or "expr" not in g:
                raise ModelError(f"actions[{i}].guards[{j}] 必须是含 expr 的对象")
            if validate_expr(g["expr"], scope_int, bool_vars, f"actions[{i}].{aname}.guard[{j}]") != "bool":
                raise ModelError(f"actions[{i}].{aname}.guard[{j}] 必须是布尔条件")
            norm_guards.append({"expr": g["expr"]})

        effects = act.get("effects", [])
        if not isinstance(effects, list):
            raise ModelError(f"actions[{i}].effects 必须是数组")
        targets_seen: set[str] = set()
        norm_effects: list[dict] = []
        for j, e in enumerate(effects):
            if not isinstance(e, dict):
                raise ModelError(f"actions[{i}].effects[{j}] 必须是对象")
            kind = e.get("kind")
            if kind == "assign":
                tgt = _check_name(e.get("target"), f"actions[{i}].effects[{j}].target")
                if tgt in targets_seen:
                    raise ModelError(f"actions[{i}].effects[{j}]: 对 {tgt} 重复赋值")
                if tgt not in int_vars:
                    raise ModelError(f"actions[{i}].effects[{j}]: assign 目标 {tgt!r} 必须是 int 状态变量")
                if validate_expr(e.get("expr"), scope_int, bool_vars,
                                 f"actions[{i}].effects[{j}].expr") != "int":
                    raise ModelError(f"actions[{i}].effects[{j}].expr 必须是整数表达式")
                targets_seen.add(tgt)
                norm_effects.append({"kind": "assign", "target": tgt, "expr": e["expr"]})
            elif kind in ("lock", "unlock"):
                tgt = _check_name(e.get("target"), f"actions[{i}].effects[{j}].target")
                if tgt in targets_seen:
                    raise ModelError(f"actions[{i}].effects[{j}]: 对 {tgt} 重复赋值")
                if tgt not in bool_vars:
                    raise ModelError(f"actions[{i}].effects[{j}]: {kind} 目标 {tgt!r} 必须是 bool 状态变量")
                targets_seen.add(tgt)
                norm_effects.append({"kind": kind, "target": tgt})
            else:
                raise ModelError(
                    f"actions[{i}].effects[{j}]: kind 必须是 {sorted(EFFECT_KINDS)}"
                )

        norm_actions.append({
            "name": aname,
            "params": norm_params,
            "guards": norm_guards,
            "effects": norm_effects,
        })

    invariant = model.get("invariant")
    if invariant is not None:
        if not isinstance(invariant, dict) or "expr" not in invariant:
            raise ModelError("invariant 必须是含 expr 的对象")
        if validate_expr(invariant["expr"], int_vars, bool_vars, "invariant.expr") != "bool":
            raise ModelError("invariant.expr 必须是布尔表达式")
        invariant = {"expr": invariant["expr"]}

    return {
        "name": name,
        "state_vars": norm_vars,
        "actions": norm_actions,
        "invariant": invariant,
    }
