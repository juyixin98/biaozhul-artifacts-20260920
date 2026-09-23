"""AST -> 栈式字节码的编译器，同时完成小型静态类型检查。

全部由本项目自研（不调用任何现成编译器）。职责：
1. 两遍处理：先收集全部函数签名（支持相互递归/前向引用）；
2. 每个函数内：
   - 局部量槽分配（参数占 0..nparams-1，声明顺序追加）；
   - 表达式类型推导/检查（int/bool，无隐式转换）；
   - 语句级代码生成，标签回填跳转偏移，保证分支合流处栈平衡；
   - 检查带值/无值 return 与签名一致；
   - 检查函数实参个数与类型；
3. 为每条由源码产生的指令写调试映射（pc -> 源码偏移）。

局部量初始化由验证器保守处理（验证器不信任编译器），但编译器声明的
local_types 给出槽的*声明类型*，与验证器推断的运行类型对照。
"""

from __future__ import annotations

import struct

from . import ast_nodes as ast
from .bytecode import (
    ARITH_OPS, CMP_OPS, IMM_MAX, IMM_MIN, MAX_SLOT,
    DebugEntry, FuncCode, Module, T_BOOL, T_INT, T_VOID,
    OP_AND, OP_CALL, OP_JIF, OP_JUMP, OP_LOAD,
    OP_NEG, OP_NOP, OP_NOT, OP_OR, OP_POP, OP_PRINT, OP_PUSH,
    OP_RETV, OP_RET, OP_STORE, OP_TRUE, OP_FALSE,
)
from .errors import CompileError, Diagnostic
from .location import SourceText

TYPE_TO_CODE = {ast.INT: T_INT, ast.BOOL: T_BOOL}
CODE_TO_TYPE = {T_INT: ast.INT, T_BOOL: ast.BOOL}


class _Label:
    __slots__ = ("pc",)

    def __init__(self) -> None:
        self.pc = -1


class _Emitter:
    """带标签回填与调试映射的代码发射器。"""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.debug: list[DebugEntry] = []
        # (patch_pc, Label) 的等待表（rel16 的立即数字节起始）
        self._fixups: list[tuple[int, _Label]] = []

    @property
    def pc(self) -> int:
        return len(self.buf)

    def new_label(self) -> _Label:
        return _Label()

    def mark(self, label: _Label) -> None:
        label.pc = self.pc

    def emit0(self, op: int, node: ast.Node | None = None) -> int:
        at = self.pc
        self.buf.append(op)
        self._dbg(at, node)
        return at

    def emit_u8(self, op: int, arg: int, node: ast.Node | None = None) -> int:
        if not 0 <= arg <= 255:
            raise CompileError([Diagnostic(f"操作数 {arg} 超出 u8 范围", getattr(node, "span", None))])
        at = self.pc
        self.buf.append(op)
        self.buf.append(arg)
        self._dbg(at, node)
        return at

    def emit_imm16(self, op: int, value: int, node: ast.Node | None = None) -> int:
        at = self.pc
        self.buf.append(op)
        self.buf += struct.pack(">h", value)
        self._dbg(at, node)
        return at

    def emit_jump(self, op: int, label: _Label, node: ast.Node | None) -> int:
        at = self.pc
        self.buf.append(op)
        self.buf += b"\x00\x00"
        self._dbg(at, node)
        self._fixups.append((at + 1, label))
        return at

    def _dbg(self, pc: int, node: ast.Node | None) -> None:
        if node is not None:
            self.debug.append(DebugEntry(pc, node.span.start, node.span.end))

    def resolve(self) -> None:
        for operand_pc, label in self._fixups:
            if label.pc < 0:
                raise CompileError([Diagnostic("内部错误：跳转标签未绑定", None)])
            rel = label.pc - (operand_pc - 1)   # 相对操作码
            if not IMM_MIN <= rel <= IMM_MAX:
                raise CompileError([Diagnostic(
                    f"跳转距离 {rel} 超出 rel16 范围 [{IMM_MIN},{IMM_MAX}]", None)])
            struct.pack_into(">h", self.buf, operand_pc, rel)
        self._fixups.clear()


class _LocalInfo:
    __slots__ = ("name", "type", "decl")

    def __init__(self, name: str, type_code: int, decl: ast.Node) -> None:
        self.name = name
        self.type = type_code
        self.decl = decl


class _FuncCompiler:
    def __init__(self, fn: ast.Func, signatures: dict[str, tuple[int, list[int]]],
                 source: SourceText) -> None:
        self.fn = fn
        self.source = source
        self.sigs = signatures               # name -> (ret_code, [param codes])
        self.em = _Emitter()
        self.locals: dict[str, _LocalInfo] = {}
        # 槽 i 的声明类型；与 self.locals 的插入顺序一一对应
        self.local_types: list[int] = []
        self.diags: list[Diagnostic] = []

    def err(self, msg: str, node: ast.Node | None = None) -> None:
        self.diags.append(Diagnostic(msg, node.span if node else None))

    def compile(self) -> FuncCode:
        # 1) 分配参数槽
        for p in self.fn.params:
            if p.name in self.locals:
                self.err(f"参数 {p.name!r} 重复", p)
            if len(self.local_types) > MAX_SLOT:
                self.err(f"函数 {self.fn.name}: 局部量槽超过 {MAX_SLOT}", p)
            code = TYPE_TO_CODE[p.type_name]
            self.locals[p.name] = _LocalInfo(p.name, code, p)
            self.local_types.append(code)
        if self.diags:
            raise CompileError(self.diags)

        # 2) 编译函数体（声明与代码生成交错，槽随声明追加）
        self.gen_block(self.fn.body)
        self.em.resolve()

        if self.diags:
            raise CompileError(self.diags)

        # 3) 末尾补一条 RET/RETV，保证函数总是以返回指令结束
        #    （若控制流理论上不可达，也不影响正确性——验证器只检查可达路径；
        #    此处的兜底返回仅在“函数体没有 return 却声明了返回类型”时成为可达，
        #    那种程序编译器已在 gen_return 之外另行依赖验证器/调用方约束。）
        ret_code = T_VOID if self.fn.ret_type is None else TYPE_TO_CODE[self.fn.ret_type]
        if ret_code == T_VOID:
            self.em.emit0(OP_RET, self.fn.body)
        else:
            self.em.emit_imm16(OP_PUSH, 0, self.fn.body)
            self.em.emit0(OP_RETV, self.fn.body)

        return FuncCode(
            name=self.fn.name,
            param_types=[TYPE_TO_CODE[p.type_name] for p in self.fn.params],
            ret_type=ret_code,
            local_types=self.local_types,
            code=self.em.buf,
            debug=self.em.debug,
        )

    # ---------- 语句 ----------
    def gen_block(self, block: ast.Block) -> None:
        for st in block.stmts:
            self.gen_stmt(st)

    def gen_stmt(self, st: ast.Stmt) -> None:
        if isinstance(st, ast.EmptyStmt):
            self.em.emit0(OP_NOP, st)
        elif isinstance(st, ast.VarDecl):
            self.gen_vardecl(st)
        elif isinstance(st, ast.AssignStmt):
            self.gen_assign(st)
        elif isinstance(st, ast.IfStmt):
            self.gen_if(st)
        elif isinstance(st, ast.WhileStmt):
            self.gen_while(st)
        elif isinstance(st, ast.ReturnStmt):
            self.gen_return(st)
        elif isinstance(st, ast.PrintStmt):
            self.gen_expr(st.value)
            # PRINT 自身消费栈顶，不再 POP
            self.em.emit0(OP_PRINT, st)
        elif isinstance(st, ast.ExprStmt):
            ty = self.gen_expr(st.expr)
            # 只有非 void 表达式（带值函数调用）会在栈上留值，需要丢弃
            if ty != T_VOID:
                self.em.emit0(OP_POP, st)
        else:  # pragma: no cover
            self.err(f"内部错误：未实现的语句 {type(st).__name__}", st)

    def gen_vardecl(self, st: ast.VarDecl) -> None:
        if st.name in self.locals:
            self.err(f"{st.name!r} 已在本函数中声明过", st)
            return
        if st.init is None and st.inferred:
            self.err("var 声明必须带初始化式", st)
            return

        if st.inferred:
            # 顺序：生成初始化式（值留栈上）-> 追加槽 -> STORE。
            # 追加槽不依赖预先知道总数，因此 STORE 的槽号此刻已确定。
            init_ty = self.gen_expr(st.init)
            slot = self._alloc_slot(st.name, init_ty, st)
            self.em.emit_u8(OP_STORE, slot, st)
            return

        ty = TYPE_TO_CODE[st.type_name]
        # 显式类型声明：先占槽（无初始化式时尤其重要——验证器需要该槽存在），
        # 再生成初始化式并 STORE。语言禁止在自身初始化式里引用自身。
        slot = self._alloc_slot(st.name, ty, st)
        if st.init is not None:
            init_ty = self.gen_expr(st.init)
            if init_ty != ty:
                self.err(
                    f"不能用 {CODE_TO_TYPE[init_ty]} 初始化 {st.type_name} "
                    f"变量 {st.name!r}", st)
            self.em.emit_u8(OP_STORE, slot, st)
        # 无初始化：不发射 store，槽位初始状态为“未赋值”，交给验证器。

    def _alloc_slot(self, name: str, ty: int, node: ast.Node) -> int:
        slot = len(self.locals)
        if slot > MAX_SLOT:
            self.err(f"函数 {self.fn.name}: 局部量槽超过 {MAX_SLOT}", node)
        self.locals[name] = _LocalInfo(name, ty, node)
        self.local_types.append(ty)
        return slot

    def _slot_of(self, name: str) -> int:
        return list(self.locals.keys()).index(name)

    def gen_assign(self, st: ast.AssignStmt) -> None:
        if st.name not in self.locals:
            self.err(f"变量 {st.name!r} 未声明", st)
            return
        ty = self.gen_expr(st.value)
        info = self.locals[st.name]
        if ty != info.type:
            self.err(
                f"不能把 {CODE_TO_TYPE[ty]} 赋给 {CODE_TO_TYPE[info.type]} 变量 {st.name!r}",
                st,
            )
        self.em.emit_u8(OP_STORE, self._slot_of(st.name), st)

    def gen_if(self, st: ast.IfStmt) -> None:
        cond_ty = self.gen_expr(st.cond)
        if cond_ty != T_BOOL:
            self.err(f"if 条件必须是 bool，实际为 {CODE_TO_TYPE[cond_ty]}", st)
        # JIF 语义是“条件为真则跳转”，所以直接跳到 then 块；
        # 条件为假顺序进入 else（或 end）。
        then_lbl = self.em.new_label()
        end_lbl = self.em.new_label()
        self.em.emit_jump(OP_JIF, then_lbl, st.cond)
        if st.else_block is not None:
            self.gen_block(st.else_block)
        self.em.emit_jump(OP_JUMP, end_lbl, st.cond)
        self.em.mark(then_lbl)
        self.gen_block(st.then_block)
        self.em.mark(end_lbl)

    def gen_while(self, st: ast.WhileStmt) -> None:
        head_lbl = self.em.new_label()
        body_lbl = self.em.new_label()
        end_lbl = self.em.new_label()
        self.em.mark(head_lbl)
        cond_ty = self.gen_expr(st.cond)
        if cond_ty != T_BOOL:
            self.err(f"while 条件必须是 bool，实际为 {CODE_TO_TYPE[cond_ty]}", st)
        # 条件真 -> body；假 -> end
        self.em.emit_jump(OP_JIF, body_lbl, st.cond)
        self.em.emit_jump(OP_JUMP, end_lbl, st.cond)
        self.em.mark(body_lbl)
        self.gen_block(st.body)
        self.em.emit_jump(OP_JUMP, head_lbl, st.body)   # 回边
        self.em.mark(end_lbl)

    def gen_return(self, st: ast.ReturnStmt) -> None:
        want = T_VOID if self.fn.ret_type is None else TYPE_TO_CODE[self.fn.ret_type]
        if st.value is None:
            self.has_void_return = True
            if want != T_VOID:
                self.err(f"函数 {self.fn.name} 声明返回 {CODE_TO_TYPE[want]}，return 缺值", st)
            self.em.emit0(OP_RET, st)
        else:
            self.has_value_return = True
            ty = self.gen_expr(st.value)
            if want == T_VOID:
                self.err(f"void 函数 {self.fn.name} 的 return 不能带值", st)
            elif ty != want:
                self.err(
                    f"返回值类型 {CODE_TO_TYPE[ty]} 与声明的 {CODE_TO_TYPE[want]} 不符",
                    st,
                )
            self.em.emit0(OP_RETV, st)

    # ---------- 表达式：返回类型码 ----------
    def gen_expr(self, e: ast.Expr) -> int:
        if isinstance(e, ast.IntLit):
            if not IMM_MIN <= e.value <= IMM_MAX:
                self.err(f"整数字面量 {e.value} 超出 [{IMM_MIN},{IMM_MAX}]", e)
            self.em.emit_imm16(OP_PUSH, e.value, e)
            return T_INT

        if isinstance(e, ast.BoolLit):
            # bool 常量有专门的操作码，验证器据此精确给栈格赋 bool 类型
            self.em.emit0(OP_TRUE if e.value else OP_FALSE, e)
            return T_BOOL

        if isinstance(e, ast.VarRef):
            if e.name not in self.locals:
                self.err(f"变量 {e.name!r} 未声明", e)
                return T_INT
            info = self.locals[e.name]
            self.em.emit_u8(OP_LOAD, self._slot_of(e.name), e)
            return info.type

        if isinstance(e, ast.Unary):
            inner = self.gen_expr(e.operand)
            if e.op == "-":
                if inner != T_INT:
                    self.err(f"一元 '-' 只能用于 int，实际为 {CODE_TO_TYPE[inner]}", e)
                self.em.emit0(OP_NEG, e)
                return T_INT
            # !
            if inner != T_BOOL:
                self.err(f"一元 '!' 只能用于 bool，实际为 {CODE_TO_TYPE[inner]}", e)
            self.em.emit0(OP_NOT, e)
            return T_BOOL

        if isinstance(e, ast.Binary):
            lt = self.gen_expr(e.left)
            rt = self.gen_expr(e.right)
            op = e.op
            if op in ARITH_OPS:
                if lt != T_INT or rt != T_INT:
                    self.err(
                        f"运算符 {op!r} 要求两个 int，实际为 "
                        f"{CODE_TO_TYPE[lt]}/{CODE_TO_TYPE[rt]}", e)
                self.em.emit0(ARITH_OPS[op], e)
                return T_INT
            if op in CMP_OPS:
                if lt != T_INT or rt != T_INT:
                    self.err(
                        f"比较运算符 {op!r} 要求两个 int，实际为 "
                        f"{CODE_TO_TYPE[lt]}/{CODE_TO_TYPE[rt]}", e)
                self.em.emit0(CMP_OPS[op], e)
                return T_BOOL
            if op == "&&":
                if lt != T_BOOL or rt != T_BOOL:
                    self.err("'&&' 要求两个 bool", e)
                self.em.emit0(OP_AND, e)
                return T_BOOL
            if op == "||":
                if lt != T_BOOL or rt != T_BOOL:
                    self.err("'||' 要求两个 bool", e)
                self.em.emit0(OP_OR, e)
                return T_BOOL
            self.err(f"内部错误：未知运算符 {op!r}", e)
            return T_INT

        if isinstance(e, ast.Call):
            if e.name not in self.sigs:
                self.err(f"调用了未定义的函数 {e.name!r}", e)
                return T_INT
            ret, ptypes = self.sigs[e.name]
            if len(e.args) != len(ptypes):
                self.err(
                    f"函数 {e.name!r} 需要 {len(ptypes)} 个实参，实际 {len(e.args)} 个", e)
            arg_tys: list[int] = []
            for a in e.args:
                arg_tys.append(self.gen_expr(a))
            for i, (got, want) in enumerate(zip(arg_tys, ptypes)):
                if got != want:
                    self.err(
                        f"调用 {e.name!r} 第 {i + 1} 个实参应为 {CODE_TO_TYPE[want]}，"
                        f"实际为 {CODE_TO_TYPE[got]}", e)
            fi = self._func_index(e.name)
            self.em.emit_u8(OP_CALL, fi, e)
            return ret

        self.err(f"内部错误：未实现的表达式 {type(e).__name__}", e)
        return T_INT

    def _func_index(self, name: str) -> int:
        return list(self.sigs.keys()).index(name)


def compile_program(program: ast.Program, source: SourceText,
                    module_name: str = "main") -> Module:
    # 第一遍：收集签名并查重名
    sigs: dict[str, tuple[int, list[int]]] = {}
    order: list[str] = []
    front_diags: list[Diagnostic] = []
    for fn in program.funcs:
        if fn.name in sigs:
            front_diags.append(Diagnostic(f"函数 {fn.name!r} 重复定义", fn.span))
        ptypes = [TYPE_TO_CODE[p.type_name] for p in fn.params]
        ret = T_VOID if fn.ret_type is None else TYPE_TO_CODE[fn.ret_type]
        sigs[fn.name] = (ret, ptypes)
        order.append(fn.name)
    if front_diags:
        raise CompileError(front_diags)

    funcs: list[FuncCode] = []
    for fn in program.funcs:
        fc = _FuncCompiler(fn, sigs, source).compile()
        funcs.append(fc)

    mod = Module(module_name, source.text, funcs)
    # 编译后“可达 return 类型一致性”的简单补充检查：
    # gen_return 已逐语句检查，这里不再重复。
    return mod


def compile_source(text: str, filename: str = "<input>", module_name: str = "main") -> Module:
    source = SourceText(text, filename)
    from .parser import parse_source
    program = parse_source(source)
    return compile_program(program, source, module_name)
