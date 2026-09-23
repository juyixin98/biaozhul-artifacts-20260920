"""从 AST 生成栈式字节码。

编译策略：
* 第一遍收集函数签名（解决前向引用 / 递归），外加两个内建函数
  ``print_int`` / ``print_bool``。
* 结构化控制流用符号标签发射跳转，函数末尾统一把标签解析成“目标指令下标”
  （:mod:`byteverifier.bytecode` 编码时再换算成字节偏移）。
* ``max_stack`` 用一个无类型的 CFG 高度不动点精确计算；验证器会独立复核。
* 每条指令记录源码 :class:`Span`，经编码/解码后挂回到 CodeObject 上，
  这样验证器报出的最短错误路径能回溯到源码行。
"""

from __future__ import annotations

from collections import deque

from . import ast_nodes as ast
from .bytecode import (
    BUILTIN_PRINT_BOOL, BUILTIN_PRINT_INT, JUMP_OPS, Module, OPERAND_SIZE,
    TAG_BOOL, TAG_INT, TAG_VOID, TYPE_NAME_TO_TAG, CodeObject, Instruction,
    decode_module,
)
from .common import PHASE_COMPILER, ToolError

# 内建函数: 调用编号 -> (名字, [(参数标签)], 返回标签)
BUILTINS = {
    BUILTIN_PRINT_INT: ("print_int", [TAG_INT], TAG_VOID),
    BUILTIN_PRINT_BOOL: ("print_bool", [TAG_BOOL], TAG_VOID),
}
BUILTIN_BY_NAME = {name: (idx, params, ret)
                   for idx, (name, params, ret) in BUILTINS.items()}


class _Label:
    _next = 0

    def __init__(self) -> None:
        self.id = _Label._next
        _Label._next += 1
        self.target_ins: int | None = None


class _FuncEmitter:
    def __init__(self, func: ast.FuncDecl, sigs: dict, source_lines: list[str]) -> None:
        self.func = func
        self.sigs = sigs                     # name -> (index, params, ret_tag)
        self.lines = source_lines
        self.insns: list[Instruction] = []
        self.consts: list[tuple[int, int | bool]] = []
        self.slots: dict[str, int] = {}
        self.slot_names: list[str] = []
        self.local_tags: list[int] = []
        self.param_tags: list[int] = []
        self._refs: list[tuple[int, _Label]] = []  # (指令下标, 目标标签)

        for ptype, pname in func.params:
            if pname in self.slots:
                raise ToolError(
                    PHASE_COMPILER, "duplicate.variable",
                    f"参数 {pname!r} 与已有局部量重名", func.span,
                )
            self._alloc(pname, TYPE_NAME_TO_TAG[ptype], is_param=True)

    # ---------- 发射原语 ----------

    def _alloc(self, name: str, tag: int, is_param: bool) -> int:
        idx = len(self.slot_names)
        self.slots[name] = idx
        self.slot_names.append(name)
        if is_param:
            self.param_tags.append(tag)
        else:
            self.local_tags.append(tag)
        return idx

    def _emit(self, op: int, span: ast.Span, operand: int = 0) -> int:
        idx = len(self.insns)
        self.insns.append(Instruction(opcode=op, operand=operand, span=span))
        return idx

    def _new_label(self) -> _Label:
        return _Label()

    def _mark(self, lab: _Label) -> None:
        lab.target_ins = len(self.insns)

    def _jump(self, op: int, lab: _Label, span) -> None:
        idx = self._emit(op, span)
        self._refs.append((idx, lab))

    def _const(self, tag: int, value) -> int:
        key = (tag, value)
        for i, c in enumerate(self.consts):
            if c == key:
                return i
        self.consts.append(key)
        return len(self.consts) - 1

    # ---------- 语句 ----------

    def emit_func(self) -> CodeObject:
        for stmt in self.func.body:
            self._stmt(stmt)
        if self.func.ret_type == "void":
            # void 函数允许直接走到末尾：补一条隐式 RET
            self._emit(0x52, self.func.span)

        # 标签先解析为指令下标，布局后统一换算成字节偏移
        label_targets: dict[int, int] = {}
        for ins_idx, lab in self._refs:
            if lab.target_ins is None:
                raise ToolError(
                    PHASE_COMPILER, "label.internal",
                    "内部错误: 跳转标签未绑定", self.func.span,
                )
            label_targets[ins_idx] = lab.target_ins

        code = CodeObject(
            name=self.func.name,
            ret_tag=TYPE_NAME_TO_TAG[self.func.ret_type],
            param_tags=self.param_tags,
            local_tags=self.local_tags,
            instructions=self.insns,
            consts=self.consts,
            slot_names=self.slot_names,
            source_lines=self.lines,
        )
        # 先做指令布局（赋 pc），追加一个“代码末尾”虚拟位置给悬空标签
        # （例如 return 之后块结束点；跳到该处等价于走到函数末尾，
        # 是否合法由验证器按返回类型裁决）
        pcs = self._layout(code)
        end_pc = pcs[-1] + 1 + OPERAND_SIZE[self.insns[-1].opcode]
        for ins_idx, target_ins in label_targets.items():
            target_pc = pcs[target_ins] if target_ins < len(pcs) else end_pc
            self.insns[ins_idx].operand = target_pc
        # 再算 max_stack / pc->span
        code.max_stack = _compute_max_stack(code, self.sigs)
        code.pc_spans = {ins.pc: ins.span for ins in code.instructions if ins.span}
        return code

    @staticmethod
    def _layout(code: CodeObject) -> list[int]:
        pcs: list[int] = []
        off = 0
        for ins in code.instructions:
            pcs.append(off)
            ins.pc = off
            off += 1 + OPERAND_SIZE[ins.opcode]
        return pcs

    def _stmt(self, s) -> None:
        if isinstance(s, ast.VarDecl):
            if s.name in self.slots:
                raise ToolError(
                    PHASE_COMPILER, "duplicate.variable",
                    f"局部量 {s.name!r} 重复声明", s.span,
                )
            slot = self._alloc(s.name, TYPE_NAME_TO_TAG[s.type], is_param=False)
            if s.init is not None:
                self._expr(s.init)
                self._emit(0x12, s.span, slot)   # STORE_LOCAL
        elif isinstance(s, ast.Assign):
            if s.name not in self.slots:
                raise ToolError(
                    PHASE_COMPILER, "unknown.variable",
                    f"变量 {s.name!r} 尚未声明就赋值（VLang 要求先声明）", s.span,
                )
            self._expr(s.value)
            self._emit(0x12, s.span, self.slots[s.name])
        elif isinstance(s, ast.ExprStmt):
            call = s.expr
            assert isinstance(call, ast.CallExpr)
            ret_tag = self._callee_ret(call.name, call)
            self._expr(call)
            if ret_tag != TAG_VOID:
                self._emit(0x13, s.span)        # POP
        elif isinstance(s, ast.IfStmt):
            end_lab = self._new_label()
            else_lab = self._new_label() if s.else_body else end_lab
            self._expr(s.cond)
            self._jump(0x41, else_lab, s.cond.span)   # JIF
            for t in s.then_body:
                self._stmt(t)
            if s.else_body:
                self._jump(0x40, end_lab, s.span)     # JMP
                self._mark(else_lab)
                for t in s.else_body:
                    self._stmt(t)
            self._mark(end_lab)
        elif isinstance(s, ast.WhileStmt):
            head_lab = self._new_label()
            end_lab = self._new_label()
            self._mark(head_lab)
            self._expr(s.cond)
            self._jump(0x41, end_lab, s.cond.span)   # JIF -> end（条件假）
            for t in s.body:
                self._stmt(t)
            self._jump(0x40, head_lab, s.span)       # 回边
            self._mark(end_lab)
        elif isinstance(s, ast.ReturnStmt):
            if s.value is not None:
                self._expr(s.value)
                self._emit(0x51, s.span)             # RETV
            else:
                self._emit(0x52, s.span)             # RET
        else:
            raise ToolError(
                PHASE_COMPILER, "ast.internal",
                f"内部错误: 不支持的语句 {type(s).__name__}", getattr(s, "span", None),
            )

    # ---------- 表达式 ----------

    def _expr(self, e) -> None:
        if isinstance(e, ast.IntLit):
            self._emit(0x10, e.span, self._const(TAG_INT, e.value))
        elif isinstance(e, ast.BoolLit):
            self._emit(0x10, e.span, self._const(TAG_BOOL, e.value))
        elif isinstance(e, ast.VarRef):
            if e.name not in self.slots:
                raise ToolError(
                    PHASE_COMPILER, "unknown.variable",
                    f"变量 {e.name!r} 未声明", e.span,
                )
            self._emit(0x11, e.span, self.slots[e.name])
        elif isinstance(e, ast.Unary):
            self._expr(e.operand)
            op = 0x24 if e.op == "-" else 0x25      # NEG / NOT
            self._emit(op, e.span)
        elif isinstance(e, ast.Binary):
            if e.op in ("&&", "||"):
                self._short_circuit(e)
            else:
                self._expr(e.left)
                self._expr(e.right)
                op = {
                    "+": 0x20, "-": 0x21, "*": 0x22, "/": 0x23,
                    "==": 0x30, "!=": 0x31, "<": 0x32,
                    ">": 0x33, "<=": 0x34, ">=": 0x35,
                }[e.op]
                self._emit(op, e.span)
        elif isinstance(e, ast.CallExpr):
            idx = self._callee_index(e)
            for a in e.args:
                self._expr(a)
            self._emit(0x50, e.span, idx)           # CALL
        else:
            raise ToolError(
                PHASE_COMPILER, "ast.internal",
                f"内部错误: 不支持的表达式 {type(e).__name__}", getattr(e, "span", None),
            )

    def _short_circuit(self, e: ast.Binary) -> None:
        # 约定 JIF = 条件为假时跳转（与 if/while 发射方式一致）。
        # a || b:  a; JIF L_eval_b; push true;  JMP L_end;
        #          L_eval_b: b; JMP L_end; L_short_true: ... 用下面等价结构：
        #   a; JIF L_short_false? —— 为保持“JIF=假则跳”，两种运算分别处理：
        #
        # && : a 为假即可短路为假 -> JIF 跳到 false 常量
        # || : a 为真即可短路为真 -> 需要“为真则跳”，用 JIF 的对偶：
        #      a; JIF L1(去求 b);  JMP L_TRUE;  L1: b; JMP Lend; L_TRUE: true; Lend
        short_lab = self._new_label()
        end_lab = self._new_label()
        self._expr(e.left)
        if e.op == "&&":
            self._jump(0x41, short_lab, e.left.span)   # 左假 -> 短路 false
            self._expr(e.right)
            self._jump(0x40, end_lab, e.span)
            self._mark(short_lab)
            self._emit(0x10, e.span, self._const(TAG_BOOL, False))
            self._mark(end_lab)
        else:
            # a; JIF L_try_b; <a 为真: push true>; JMP L_end;
            # L_try_b: b; L_end:
            try_b = self._new_label()
            self._jump(0x41, try_b, e.left.span)
            self._emit(0x10, e.span, self._const(TAG_BOOL, True))
            self._jump(0x40, end_lab, e.span)
            self._mark(try_b)
            self._expr(e.right)
            self._mark(end_lab)

    # ---------- 调用解析 ----------

    def _callee_index(self, call: ast.CallExpr) -> int:
        if call.name in self.sigs:
            idx, params, _ = self.sigs[call.name]
            if len(call.args) != len(params):
                raise ToolError(
                    PHASE_COMPILER, "arg.count",
                    f"函数 {call.name!r} 需要 {len(params)} 个参数，"
                    f"实际给了 {len(call.args)} 个", call.span,
                )
            return idx
        if call.name in BUILTIN_BY_NAME:
            bidx, params, _ = BUILTIN_BY_NAME[call.name]
            if len(call.args) != len(params):
                raise ToolError(
                    PHASE_COMPILER, "arg.count",
                    f"内建函数 {call.name!r} 需要 {len(params)} 个参数，"
                    f"实际给了 {len(call.args)} 个", call.span,
                )
            return bidx
        raise ToolError(
            PHASE_COMPILER, "unknown.func",
            f"调用了未定义的函数 {call.name!r}", call.span,
        )

    def _callee_ret(self, name: str, call: ast.CallExpr) -> int:
        if name in self.sigs:
            return self.sigs[name][2]
        if name in BUILTIN_BY_NAME:
            return BUILTIN_BY_NAME[name][2]
        raise ToolError(
            PHASE_COMPILER, "unknown.func",
            f"调用了未定义的函数 {name!r}", call.span,
        )


def _compute_max_stack(code: CodeObject, sigs: dict) -> int:
    """无类型 CFG 高度不动点：求每条指令入口高度与全程最大高度。

    跳转在“转移之后”的高度生效（JIF 已弹出条件）。
    """
    by_pc = {ins.pc: ins for ins in code.instructions}
    if not code.instructions:
        return 0
    code_len = code.instructions[-1].pc + 1 + OPERAND_SIZE[
        code.instructions[-1].opcode]
    heights: dict[int, int] = {0: 0}
    queue = deque([0])
    max_h = 0
    while queue:
        pc = queue.popleft()
        ins = by_pc[pc]
        h = heights[pc]

        def succ(target: int, th: int) -> None:
            nonlocal max_h
            max_h = max(max_h, th)
            old = heights.get(target)
            if old is None or old < th:
                heights[target] = th
                queue.append(target)

        op = ins.opcode
        target_pc: int | None = None
        if op in JUMP_OPS:
            target_pc = ins.operand     # 已是字节偏移
        # 弹出效应
        if op in (0x20, 0x21, 0x22, 0x23, 0x30, 0x31, 0x32,
                  0x33, 0x34, 0x35):
            h -= 2
        elif op in (0x24, 0x25, 0x12, 0x13, 0x41):
            h -= 1
        elif op == 0x50:
            if ins.operand in BUILTINS:
                arity = len(BUILTINS[ins.operand][1])
                ret = BUILTINS[ins.operand][2]
            else:
                arity, ret = _SIG_CACHE[ins.operand]
            h -= arity
            if ret != TAG_VOID:
                h += 1
        # 压入效应
        if op in (0x10, 0x11):
            h += 1
        elif op in (0x20, 0x21, 0x22, 0x23, 0x24):
            h += 1
        elif op in (0x25, 0x30, 0x31, 0x32, 0x33, 0x34, 0x35):
            h += 1

        max_h = max(max_h, h)
        next_pc = pc + 1 + OPERAND_SIZE[op]
        if op == 0x40:                    # JMP
            if target_pc == code_len:
                pass                      # 跳到函数末尾，没有后继指令
            else:
                succ(target_pc, h)
        elif op == 0x41:                  # JIF（条件弹出已在上面处理）
            if target_pc != code_len:
                succ(target_pc, h)
            if next_pc in by_pc:
                succ(next_pc, h)
        elif op in (0x51, 0x52):          # RETV / RET 终止
            pass
        else:
            if next_pc in by_pc:
                succ(next_pc, h)
    return max_h


# 供 _compute_max_stack 使用的“最近一次编译的签名缓存”。
# 模块函数调用编号即函数下标；编译一个模块期间签名不变。
_SIG_CACHE: list[tuple[int, int]] = []


def build_module(tree: list[ast.FuncDecl], source: str,
                 filename: str = "<input>") -> Module:
    """AST -> 已解码的 :class:`Module`（可直接交给验证器/解释器）。"""
    global _SIG_CACHE

    # 第一遍：函数签名
    sigs: dict[str, tuple[int, list[int], int]] = {}
    names: set[str] = set()
    for i, f in enumerate(tree):
        if f.name in names:
            raise ToolError(
                PHASE_COMPILER, "duplicate.func",
                f"函数 {f.name!r} 重复定义", f.span,
            )
        if f.name in BUILTIN_BY_NAME:
            raise ToolError(
                PHASE_COMPILER, "builtin.name",
                f"函数名 {f.name!r} 是内建保留名", f.span,
            )
        names.add(f.name)
        params = [TYPE_NAME_TO_TAG[t] for t, _ in f.params]
        sigs[f.name] = (i, params, TYPE_NAME_TO_TAG[f.ret_type])
    _SIG_CACHE = [(len(p), r) for _, (_, p, r) in sigs.items()]

    source_lines = source.splitlines()
    raw_codes: list[CodeObject] = []
    for f in tree:
        raw_codes.append(_FuncEmitter(f, sigs, source_lines).emit_func())

    raw_mod = Module(raw_codes)
    data = raw_mod.encode()
    mod = decode_module(data)
    # 挂回调试信息（解码后的对象不带这些非序列化字段）
    for fresh, orig in zip(mod.functions, raw_codes):
        fresh.slot_names = orig.slot_names
        fresh.pc_spans = dict(orig.pc_spans)
        fresh.source_lines = source_lines
        fresh.filename = filename
    return mod


def compile_source(source: str, filename: str = "<input>") -> Module:
    from .parser import parse_source
    tree = parse_source(source, filename)
    return build_module(tree, source, filename)
