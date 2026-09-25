"""自研中间表示（IR）。

设计目标：足够低层，使过程内数据流分析只需要顺序处理线性指令；同时保留
AST 的全部控制结构（以标签基本块 CFG 表示）与源码位置。

指令集合（线性、三地址风格）::

    Const( dst, value)                 dst = 字面量
    Copy(  dst, src_operand)           dst = 变量/临时量
    Binop( dst, op, left, right)       dst = left OP right
    Unop(  dst, op, operand)           dst = OP operand
    Source(dst)                        dst = source()           （污点入口）
    Sink(  arg)                        sink(arg)               （汇）
    Sanitize(dst, arg)                 dst = clean(arg)        （清洗器）
    Call(  dst, name, args, builtin)   dst = f(a, b, ...)
    Ret(   operand | None)             return ...
    Jump(target) / Br(cond, t, f)      终结指令：无条件/条件跳转

``Call`` 统一表示普通调用；``builtin`` 标记 source/sanitizer（sink 无返回值，
单独用 Sink），未知函数与用户函数都走 Call，由 analyzer 区分。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import ast_nodes as ast
from .config import AnalysisConfig
from .errors import AnalysisError
from .location import Span


# ---------------- 操作数与指令 ----------------


@dataclass(frozen=True)
class Operand:
    kind: str  # 'var' | 'const'
    value: object  # var -> str; const -> int/str/bool
    span: Span

    def as_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value}


@dataclass(frozen=True)
class Instruction:
    op: str
    span: Span
    # 不同指令使用不同字段；用 tuple/frozenset 保证可散列（摘要缓存需要）
    dst: Optional[str] = None
    operands: Tuple[Operand, ...] = ()
    binop: Optional[str] = None
    unop: Optional[str] = None
    call_name: Optional[str] = None
    builtin: Optional[str] = None  # 'source' | 'sanitize' | None
    targets: Tuple[str, ...] = ()
    const_value: object = None

    def as_dict(self) -> dict:
        d = {"op": self.op}
        if self.dst is not None:
            d["dst"] = self.dst
        if self.operands:
            d["operands"] = [o.as_dict() for o in self.operands]
        if self.binop:
            d["binop"] = self.binop
        if self.unop:
            d["unop"] = self.unop
        if self.call_name is not None:
            d["call"] = self.call_name
        if self.targets:
            d["targets"] = list(self.targets)
        if self.const_value is not None:
            d["value"] = self.const_value
        d["span"] = str(self.span)
        return d


@dataclass
class BasicBlock:
    label: str
    instructions: List[Instruction] = field(default_factory=list)
    successors: List[str] = field(default_factory=list)


@dataclass
class IRFunction:
    name: str
    params: List[str]
    blocks: Dict[str, BasicBlock]  # label -> block，插入序即 CFG 序
    order: List[str]
    span: Span
    name_span: Span


@dataclass
class IRProgram:
    functions: Dict[str, IRFunction]
    warnings: List[dict] = field(default_factory=list)


# ---------------- AST -> IR ----------------


class IRBuilder:
    def __init__(self, program: ast.Program, config: AnalysisConfig):
        self.program = program
        self.config = config
        self._tmp_counter = 0
        self._label_counter = 0
        self.cur: Optional[BasicBlock] = None
        self.fn: Optional[IRFunction] = None
        self.blocks: Dict[str, BasicBlock] = {}
        self.order: List[str] = []
        self.warnings: List[dict] = []
        self.user_fn_names = {f.name for f in program.functions}

    def build(self) -> IRProgram:
        funcs: Dict[str, IRFunction] = {}
        for fn in self.program.functions:
            if fn.name in funcs:
                raise AnalysisError(f"函数 {fn.name!r} 重复定义", fn.span)
            if fn.name in self.config.sources | self.config.sinks | self.config.sanitizers:
                raise AnalysisError(
                    f"函数名 {fn.name!r} 与内置源/汇/清洗器重名，请改名", fn.name_span
                )
            ir_fn = self._build_function(fn)
            funcs[fn.name] = ir_fn
        return IRProgram(funcs, self.warnings)

    # ---- 基本块管理 ----
    def _new_label(self, hint: str = "L") -> str:
        self._label_counter += 1
        return f"{hint}{self._label_counter}"

    def _new_tmp(self) -> str:
        self._tmp_counter += 1
        return f"__t{self._tmp_counter}"

    def _new_block(self, hint: str = "L") -> BasicBlock:
        label = self._new_label(hint)
        bb = BasicBlock(label)
        self.blocks[label] = bb
        self.order.append(label)
        return bb

    def _emit(self, ins: Instruction) -> None:
        assert self.cur is not None
        self.cur.instructions.append(ins)

    def _terminated(self) -> bool:
        assert self.cur is not None
        return bool(self.cur.instructions and self.cur.instructions[-1].op in ("jump", "br", "ret"))

    def _add_edge(self, target: str) -> None:
        assert self.cur is not None
        if target not in self.cur.successors:
            self.cur.successors.append(target)

    def _build_function(self, fn_ast: ast.Function) -> IRFunction:
        self._tmp_counter = 0
        self._label_counter = 0
        self.blocks = {}
        self.order = []
        self.fn = None
        entry = self._new_block("entry")
        self.cur = entry
        self.fn = IRFunction(fn_ast.name, list(fn_ast.params), self.blocks, self.order,
                             fn_ast.span, fn_ast.name_span)
        self._collect_assigned(fn_ast)
        for stmt in fn_ast.body:
            if self._terminated():
                # 死代码：仍做降级以暴露其中的名称错误等，但不连接 CFG
                dead = self._new_block("dead")
                self.cur = dead
            self._emit_stmt(stmt)
        if not self._terminated():
            self._emit(Instruction("ret", fn_ast.span))
        return self.fn

    # ---- 名称分析（仅用于“从未赋值”的告警，不影响污点正确性）----
    def _collect_assigned(self, fn_ast: ast.Function) -> None:
        assigned = set()

        def walk_expr(e: ast.Expr) -> None:
            for child in _expr_children(e):
                walk_expr(child)

        def walk_stmt(s: ast.Stmt) -> None:
            if isinstance(s, ast.Assign):
                assigned.add(s.target)
                walk_expr(s.value)
            elif isinstance(s, ast.ExprStmt):
                walk_expr(s.expr)
            elif isinstance(s, ast.Return):
                if s.value is not None:
                    walk_expr(s.value)
            elif isinstance(s, ast.If):
                walk_expr(s.cond)
                for st in s.then_body + s.else_body:
                    walk_stmt(st)
            elif isinstance(s, ast.While):
                walk_expr(s.cond)
                for st in s.body:
                    walk_stmt(st)

        for s in fn_ast.body:
            walk_stmt(s)
        params = set(fn_ast.params)

        def check_expr(e: ast.Expr) -> None:
            if isinstance(e, ast.Name) and e.name not in params and e.name not in assigned:
                self.warnings.append({
                    "kind": "uninitialized_read",
                    "message": f"变量 {e.name!r} 在函数 {fn_ast.name!r} 中可能未赋值"
                               f"即被读取，按干净空值处理（请确认这不是遗漏的赋值）",
                    "span": str(e.span),
                })
            for child in _expr_children(e):
                check_expr(child)

        def check_stmt(s: ast.Stmt) -> None:
            if isinstance(s, ast.Assign):
                check_expr(s.value)
            elif isinstance(s, ast.ExprStmt):
                check_expr(s.expr)
            elif isinstance(s, ast.Return) and s.value is not None:
                check_expr(s.value)
            elif isinstance(s, ast.If):
                check_expr(s.cond)
                for st in s.then_body + s.else_body:
                    check_stmt(st)
            elif isinstance(s, ast.While):
                check_expr(s.cond)
                for st in s.body:
                    check_stmt(st)

        for s in fn_ast.body:
            check_stmt(s)

    # ---- 语句降级 ----
    def _emit_stmt(self, s: ast.Stmt) -> None:
        if isinstance(s, ast.Assign):
            val = self._emit_expr(s.value)
            self._emit(Instruction("copy", s.span, dst=s.target, operands=(val,)))
        elif isinstance(s, ast.ExprStmt):
            self._emit_expr(s.expr)
        elif isinstance(s, ast.Return):
            if s.value is None:
                self._emit(Instruction("ret", s.span))
            else:
                val = self._emit_expr(s.value)
                self._emit(Instruction("ret", s.span, operands=(val,)))
        elif isinstance(s, ast.If):
            self._emit_if(s)
        elif isinstance(s, ast.While):
            self._emit_while(s)
        else:  # pragma: no cover
            raise AnalysisError(f"内部错误：未知语句 {type(s)}", s.span)

    def _emit_if(self, s: ast.If) -> None:
        cond = self._emit_expr(s.cond)
        then_bb = self._new_block("then")
        else_bb = self._new_block("else")
        join_bb = self._new_block("join")
        self._emit(Instruction("br", s.span, operands=(cond,), targets=(then_bb.label, else_bb.label)))
        self._add_edge(then_bb.label)
        self._add_edge(else_bb.label)

        self.cur = then_bb
        for st in s.then_body:
            self._emit_stmt(st)
        if not self._terminated():
            self._emit(Instruction("jump", s.span, targets=(join_bb.label,)))
            self._add_edge(join_bb.label)

        self.cur = else_bb
        for st in s.else_body:
            self._emit_stmt(st)
        if not self._terminated():
            self._emit(Instruction("jump", s.span, targets=(join_bb.label,)))
            self._add_edge(join_bb.label)

        self.cur = join_bb

    def _emit_while(self, s: ast.While) -> None:
        head_bb = self._new_block("while_head")
        body_bb = self._new_block("while_body")
        exit_bb = self._new_block("while_exit")
        self._emit(Instruction("jump", s.span, targets=(head_bb.label,)))
        self._add_edge(head_bb.label)

        self.cur = head_bb
        cond = self._emit_expr(s.cond)
        self._emit(Instruction("br", s.span, operands=(cond,), targets=(body_bb.label, exit_bb.label)))
        self._add_edge(body_bb.label)
        self._add_edge(exit_bb.label)

        self.cur = body_bb
        for st in s.body:
            self._emit_stmt(st)
        if not self._terminated():
            self._emit(Instruction("jump", s.span, targets=(head_bb.label,)))
            self._add_edge(head_bb.label)

        self.cur = exit_bb

    # ---- 表达式降级 ----
    def _const(self, value: object, span: Span) -> Operand:
        return Operand("const", value, span)

    def _emit_expr(self, e: ast.Expr) -> Operand:
        if isinstance(e, ast.NumberLit):
            return self._const(e.value, e.span)
        if isinstance(e, ast.StringLit):
            return self._const(e.value, e.span)
        if isinstance(e, ast.BoolLit):
            return self._const(e.value, e.span)
        if isinstance(e, ast.Name):
            return Operand("var", e.name, e.span)
        if isinstance(e, ast.Unary):
            v = self._emit_expr(e.operand)
            dst = self._new_tmp()
            self._emit(Instruction("unop", e.span, dst=dst, unop=e.op, operands=(v,)))
            return Operand("var", dst, e.span)
        if isinstance(e, ast.Binary):
            l = self._emit_expr(e.left)
            r = self._emit_expr(e.right)
            dst = self._new_tmp()
            self._emit(Instruction("binop", e.span, dst=dst, binop=e.op, operands=(l, r)))
            return Operand("var", dst, e.span)
        if isinstance(e, ast.Call):
            return self._emit_call(e)
        raise AnalysisError(f"内部错误：未知表达式 {type(e)}", e.span)  # pragma: no cover

    def _emit_call(self, e: ast.Call) -> Operand:
        args = [self._emit_expr(a) for a in e.args]
        if e.name in self.config.sources:
            if args:
                raise AnalysisError(f"源 {e.name!r} 不接受参数", e.span)
            dst = self._new_tmp()
            self._emit(Instruction("source", e.span, dst=dst, builtin="source"))
            return Operand("var", dst, e.span)
        if e.name in self.config.sinks:
            if len(args) != 1:
                raise AnalysisError(f"汇 {e.name!r} 恰好接受 1 个参数（实参 {len(args)} 个）", e.span)
            self._emit(Instruction("sink", e.span, operands=(args[0],), call_name=e.name))
            # sink 是语句型内置；在表达式位置使用时给一个不可信常量占位
            return self._const(0, e.span)
        if e.name in self.config.sanitizers:
            if len(args) != 1:
                raise AnalysisError(
                    f"清洗器 {e.name!r} 恰好接受 1 个参数（实参 {len(args)} 个）", e.span
                )
            dst = self._new_tmp()
            self._emit(
                Instruction("sanitize", e.span, dst=dst, operands=(args[0],),
                            builtin="sanitize")
            )
            return Operand("var", dst, e.span)
        # 用户函数 / 未知函数
        dst = self._new_tmp()
        if e.name in self.user_fn_names:
            params = next(f.params for f in self.program.functions if f.name == e.name)
            if len(args) != len(params):
                raise AnalysisError(
                    f"函数 {e.name!r} 需要 {len(params)} 个参数，实参 {len(args)} 个", e.span
                )
        elif self.config.conservative_unknown_calls:
            self.warnings.append({
                "kind": "unknown_call",
                "message": (
                    f"调用了未定义函数 {e.name!r}：保守地认为其返回值可能携带任一污点实参"
                ),
                "span": str(e.span),
            })
        else:
            raise AnalysisError(f"调用了未定义函数 {e.name!r}", e.name_span)
        self._emit(
            Instruction("call", e.span, dst=dst, call_name=e.name, operands=tuple(args))
        )
        return Operand("var", dst, e.span)


def _expr_children(e: ast.Expr):
    if isinstance(e, ast.Unary):
        return [e.operand]
    if isinstance(e, ast.Binary):
        return [e.left, e.right]
    if isinstance(e, ast.Call):
        return list(e.args)
    return []
