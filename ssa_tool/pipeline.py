"""端到端流水线与 AST 的 JSON 序列化。"""

from __future__ import annotations

from dataclasses import is_dataclass
from typing import Any

from . import ast_nodes as ast
from .errors import ToolchainError
from .interpreter import ExecResult, interpret
from .ir import FunctionIR, dump_function
from .ir_builder import build_ir
from .parser import parse
from .phi_elimination import eliminate_phis
from .ssa_construction import construct_ssa
from .ssa_validate import SSAViolation, validate_ssa


# ===================== AST -> dict =====================

def ast_to_dict(node: Any) -> Any:
    """通用 dataclass AST 序列化，Span 用自身 as_dict。"""
    if node is None or isinstance(node, (str, int, bool, float)):
        return node
    if isinstance(node, list):
        return [ast_to_dict(x) for x in node]
    if isinstance(node, ast.Span):
        return node.as_dict()
    if is_dataclass(node):
        d: dict[str, Any] = {"node": type(node).__name__}
        for k, v in node.__dict__.items():
            d[k] = ast_to_dict(v)
        return d
    return repr(node)


# ===================== 流水线 =====================

def compile_pipeline(source: str, file: str = "<src>",
                     args: list[int] | None = None,
                     run: bool = True, max_steps: int = 1_000_000,
                     ) -> dict:
    """跑完全部阶段，返回可 JSON 化的结果字典。

    阶段：parse -> raw IR -> SSA -> 校验 -> φ 消除 -> 解释执行对照。
    """
    program = parse(source, file)
    raw = build_ir(program)
    ssa = construct_ssa(raw.copy())
    violations = validate_ssa(ssa, raise_on_error=False)
    exec_fn = eliminate_phis(ssa.copy())

    result: dict[str, Any] = {
        "ok": not violations,
        "ast": ast_to_dict(program),
        "raw_ir": {"json": raw.as_dict(), "text": dump_function(raw)},
        "ssa_ir": {"json": ssa.as_dict(), "text": dump_function(ssa)},
        "exec_ir": {"json": exec_fn.as_dict(), "text": dump_function(exec_fn)},
        "ssa_violations": [str(v) for v in violations],
    }

    if run:
        runs: dict[str, Any] = {}
        for flavor, fn in (("raw", raw), ("ssa", ssa), ("exec", exec_fn)):
            r: ExecResult = interpret(fn, args, max_steps=max_steps)
            runs[flavor] = {"value": r.value, "steps": r.steps}
        runs["equivalent"] = (
            runs["raw"]["value"] == runs["ssa"]["value"] == runs["exec"]["value"]
        )
        result["execution"] = runs
        result["ok"] = result["ok"] and runs["equivalent"]
    return result


def error_payload(exc: ToolchainError | Exception) -> dict:
    return {"ok": False, "error": type(exc).__name__, "message": str(exc)}
