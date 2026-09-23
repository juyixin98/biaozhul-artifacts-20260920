"""端到端流水线便捷入口：源码 -> 解析 -> CFG -> SSA。

CLI 与 JSON 服务在此基础上分别调用 :mod:`constprop.sccp`、
:mod:`constprop.optimizer` 与两个解释器。
"""

from __future__ import annotations

from dataclasses import dataclass

from .cfg import build_cfg
from .model import CFG, serialize_cfg
from .parser import parse_source
from .source import L0Error, SourceText
from .ssa import construct_ssa


@dataclass
class CompiledProgram:
    source: SourceText
    program: object          # ast.Program
    cfg: CFG               # SSA 形式（可继续被分析/优化）
    ssa_snapshot: dict     # 当前 SSA IR 的 JSON 快照


def compile_source(text: str, filename: str = "<input>") -> CompiledProgram:
    source = SourceText(text, filename)
    program = parse_source(source)
    cfg = build_cfg(program, source)
    construct_ssa(cfg)
    return CompiledProgram(source, program, cfg, serialize_cfg(cfg.clone()))


def render_error(err: L0Error) -> dict:
    """把词法/语法/运行时错误渲染成结构化 JSON。"""
    loc = None
    if err.span is not None:
        loc = {"line": err.span.start_line, "col": err.span.start_col,
               "span": err.span.to_dict()}
    return {"error": err.kind, "message": err.message, "location": loc}
