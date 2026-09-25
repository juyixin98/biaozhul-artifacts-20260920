"""原始 IR 构建器：AST -> 内存风格整数 IR（含分支与循环）。

策略：

- 每个源变量（含参数）对应一个 ``alloc`` 槽 ``%x``，读写用 ``load/store``；
- 表达式结果放在临时寄存器 ``%tN``；
- ``if`` 生成菱形（条件块/then/else/merge），``while`` 生成条件头块+循环体+边；
- 逻辑运算符 ``&&`` / ``||`` 短路求值（也走分支 + 槽），一元 ``!`` 为与 0 比较；
- 块无显式终结指令时：从入口可达的补 ``ret 0``，不可达的补 ``unreachable``，
  从而在前端层面就能产生"不可达块"。
"""

from __future__ import annotations

from . import ast_nodes as ast
from .errors import IRError
from .ir import BINARY_OPS, Block, Const, FunctionIR, Instr, Name, Operand

# AST 二元运算符 -> IR opcode
BIN_MAP = {
    "+": "add", "-": "sub", "*": "mul", "/": "div", "%": "mod",
    "<": "lt", "<=": "le", ">": "gt", ">=": "ge",
    "==": "eq", "!=": "ne",
}


class IRBuilder:
    def __init__(self, program: ast.Program):
        self.program = program
        self.func_ast = program.functions[0]
        self.temp_counter = 0
        self.slot_counter = 0
        self.block_counter = 0
        self.blocks: list[Block] = []
        self.entry = Block("entry")
        self.blocks.append(self.entry)
        self.cur = self.entry

    # ---------------- 命名辅助 ----------------

    def new_temp(self) -> str:
        name = f"t{self.temp_counter}"
        self.temp_counter += 1
        return name

    def new_slot(self, hint: str) -> str:
        """生成与临时寄存器不重名的合成内存槽名。"""
        name = f"{hint}.s{self.slot_counter}"
        self.slot_counter += 1
        return name

    def new_block(self, hint: str) -> Block:
        name = f"{hint}.{self.block_counter}"
        self.block_counter += 1
        return Block(name)

    def _emit(self, instr: Instr) -> str | None:
        if self.cur.is_terminated():
            raise IRError(
                f"块 @{self.cur.name} 已终结，无法继续发射指令（构造器内部错误）"
            )
        if instr.op in ("jmp", "br", "ret", "unreachable"):
            self.cur.terminator = instr
        else:
            self.cur.instrs.append(instr)
        return instr.dest

    # ---------------- 入口 ----------------

    def build(self) -> FunctionIR:
        params = [p.name for p in self.func_ast.params]
        # 1) 所有内存槽先 alloc（参数槽 + var 声明槽），保证入口块 alloc 在最前。
        #    参数槽初值随后由 param + store 填充；var 初值在语句生成前 store。
        param_set = set(params)
        for p in self.func_ast.params:
            self._emit(Instr("alloc", p.name, [Const(0)], span=p.name_span))
        declared = set(params)
        for node in _walk_stmts(self.func_ast.body):
            if isinstance(node, ast.VarDecl) and node.name not in declared:
                declared.add(node.name)
                init = Const(0)
                self._emit(Instr("alloc", node.name, [init], span=node.span))

        # 2) 参数：param 取值后 store 进对应槽
        for i, p in enumerate(self.func_ast.params):
            pv = self.new_temp()
            self._emit(Instr("param", pv, [Const(i)], span=None))
            self._emit(Instr("store", None, [Name(pv)], slot=p.name,
                             span=p.name_span))

        # 3) 初值在语句生成阶段（gen_stmt VarDecl）发射，这里不再处理

        # 4) 语句
        for stmt in self.func_ast.body:
            self.gen_stmt(stmt)

        # 5) 收尾
        self._finalize_open_blocks()
        fn = FunctionIR(self.func_ast.name, params, self.blocks, flavor="raw")
        self._validate_structure(fn)
        return fn

    def _finalize_open_blocks(self) -> None:
        reachable = _reachable_from_entry(self.blocks)
        for b in self.blocks:
            if b.terminator is None:
                if b.name in reachable:
                    b.terminator = Instr("ret", None, [Const(0)])
                else:
                    b.terminator = Instr("unreachable")

    def _validate_structure(self, fn: FunctionIR) -> None:
        labels = {b.name for b in fn.blocks}
        for b in fn.blocks:
            if b.terminator is None:
                raise IRError(f"块 @{b.name} 缺少终结指令")
            for s in b.successors():
                if s not in labels:
                    raise IRError(f"@{b.name} 跳转到不存在的块 @{s}")

    # ---------------- 语句 ----------------

    def gen_stmt(self, stmt: ast.Stmt) -> None:
        if isinstance(stmt, ast.VarDecl):
            # 槽已在入口 alloc；初值在声明“执行到”时写入，
            # 因此循环体内的 var 每次迭代都会正确重置。
            if stmt.init is not None:
                val = self.gen_expr(stmt.init)
                self._emit(Instr("store", None, [val], slot=stmt.name,
                                 span=stmt.span))
            return
        if isinstance(stmt, ast.Assign):
            val = self.gen_expr(stmt.value)
            self._emit(Instr("store", None, [val], slot=stmt.name, span=stmt.span))
            return
        if isinstance(stmt, ast.ExprStmt):
            self.gen_expr(stmt.expr)
            return
        if isinstance(stmt, ast.Return):
            if stmt.value is not None:
                val = self.gen_expr(stmt.value)
            else:
                val = Const(0)
            self._emit(Instr("ret", None, [val], span=stmt.span))
            return
        if isinstance(stmt, ast.If):
            self.gen_if(stmt)
            return
        if isinstance(stmt, ast.While):
            self.gen_while(stmt)
            return
        raise IRError(f"不支持的语句 {type(stmt).__name__}")

    def _gen_stmts(self, stmts: list[ast.Stmt]) -> None:
        for s in stmts:
            if self.cur.is_terminated():
                # 终结之后的语句放进一个新鲜的不可达块，保留以便分析
                dead = self.new_block("dead")
                self.blocks.append(dead)
                self.cur = dead
            self.gen_stmt(s)

    def gen_if(self, node: ast.If) -> None:
        cond = self.gen_expr(node.cond)
        then_b = self.new_block("then")
        merge_b = self.new_block("merge")
        else_b = self.new_block("else") if node.else_body is not None else merge_b
        self._emit(Instr("br", None, [cond],
                         blocks=[then_b.name, else_b.name], span=node.cond_span))

        self.blocks.append(then_b); self.cur = then_b
        self._gen_stmts(node.then_body)
        then_open = not self.cur.is_terminated()
        if then_open:
            self._emit(Instr("jmp", None, blocks=[merge_b.name]))

        if node.else_body is not None:
            self.blocks.append(else_b); self.cur = else_b
            self._gen_stmts(node.else_body)
            if not self.cur.is_terminated():
                self._emit(Instr("jmp", None, blocks=[merge_b.name]))

        # merge 可能不可达（then/else 都 return），仍然挂上去
        self.blocks.append(merge_b)
        self.cur = merge_b

    def gen_while(self, node: ast.While) -> None:
        head = self.new_block("while.head")
        body_b = self.new_block("while.body")
        exit_b = self.new_block("while.exit")
        self._emit(Instr("jmp", None, blocks=[head.name]))

        self.blocks.append(head); self.cur = head
        cond = self.gen_expr(node.cond)
        self._emit(Instr("br", None, [cond], blocks=[body_b.name, exit_b.name],
                         span=node.cond_span))

        self.blocks.append(body_b); self.cur = body_b
        self._gen_stmts(node.body)
        if not self.cur.is_terminated():
            self._emit(Instr("jmp", None, blocks=[head.name]))
        # 若 body 终结后没有回边，head 仍由前置跳转可达；exit 可能不可达

        self.blocks.append(exit_b)
        self.cur = exit_b

    # ---------------- 表达式 ----------------

    def gen_expr(self, e: ast.Expr) -> Operand:
        if isinstance(e, ast.IntLit):
            dst = self.new_temp()
            self._emit(Instr("const", dst, [Const(e.value)], span=e.span))
            return Name(dst)
        if isinstance(e, ast.BoolLit):
            dst = self.new_temp()
            self._emit(Instr("const", dst, [Const(1 if e.value else 0)], span=e.span))
            return Name(dst)
        if isinstance(e, ast.VarRef):
            dst = self.new_temp()
            self._emit(Instr("load", dst, slot=e.name, span=e.span))
            return Name(dst)
        if isinstance(e, ast.Unary):
            if e.op == "-":
                v = self.gen_expr(e.operand)
                dst = self.new_temp()
                self._emit(Instr("neg", dst, [v], span=e.span))
                return Name(dst)
            if e.op == "!":
                v = self.gen_expr(e.operand)
                dst = self.new_temp()
                self._emit(Instr("eq", dst, [v, Const(0)], span=e.span))
                return Name(dst)
            raise IRError(f"不支持的一元运算 {e.op}")
        if isinstance(e, ast.Binary):
            return self.gen_binary(e)
        raise IRError(f"不支持的表达式 {type(e).__name__}")

    def gen_binary(self, e: ast.Binary) -> Operand:
        # 短路 && ：x != 0 ? (y != 0) : 0
        if e.op == "&&":
            return self.gen_short_circuit(e, left_zero_means_false=True,
                                          right_is_and=True)
        if e.op == "||":
            return self.gen_short_circuit(e, left_zero_means_false=False,
                                          right_is_and=False)
        left = self.gen_expr(e.left)
        right = self.gen_expr(e.right)
        op = BIN_MAP[e.op]
        if op not in BINARY_OPS:
            raise IRError(f"不支持的二元运算 {e.op}")
        dst = self.new_temp()
        self._emit(Instr(op, dst, [left, right], span=e.span))
        return Name(dst)

    def gen_short_circuit(self, e: ast.Binary, left_zero_means_false: bool,
                          right_is_and: bool) -> Operand:
        # 槽 + 控制流实现短路：
        #   && —— 左非零才算右，短路结果为 0
        #   || —— 左为零才算右，短路结果为 1
        slot = self.new_slot("land" if right_is_and else "lor")
        self._emit(Instr("alloc", slot, [Const(0)], span=e.span))
        left = self.gen_expr(e.left)
        test = self.new_temp()
        if left_zero_means_false:
            self._emit(Instr("ne", test, [left, Const(0)], span=e.span))
        else:
            self._emit(Instr("eq", test, [left, Const(0)], span=e.span))
        rhs_b = self.new_block("land.rhs" if right_is_and else "lor.rhs")
        short_b = self.new_block("land.short" if right_is_and else "lor.short")
        fin_b = self.new_block("land.end" if right_is_and else "lor.end")
        self._emit(Instr("br", None, [Name(test)], blocks=[rhs_b.name, short_b.name]))

        # 右分支：结果 = (right != 0)
        self.blocks.append(rhs_b); self.cur = rhs_b
        right = self.gen_expr(e.right)
        flag = self.new_temp()
        self._emit(Instr("ne", flag, [right, Const(0)], span=e.span))
        self._emit(Instr("store", None, [Name(flag)], slot=slot, span=e.span))
        self._emit(Instr("jmp", None, blocks=[fin_b.name]))

        # 短路分支：&& -> 0，|| -> 1
        self.blocks.append(short_b); self.cur = short_b
        short_val = self.new_temp()
        self._emit(Instr("const", short_val,
                         [Const(0 if right_is_and else 1)], span=e.span))
        self._emit(Instr("store", None, [Name(short_val)], slot=slot, span=e.span))
        self._emit(Instr("jmp", None, blocks=[fin_b.name]))

        self.blocks.append(fin_b); self.cur = fin_b
        dst = self.new_temp()
        self._emit(Instr("load", dst, slot=slot, span=e.span))
        return Name(dst)


# ---------------- 遍历/可达性 ----------------

def _walk_stmts(stmts: list[ast.Stmt]):
    """按声明顺序（深度优先）产出语句节点。"""
    for s in stmts:
        yield s
        if isinstance(s, ast.If):
            yield from _walk_stmts(s.then_body)
            if s.else_body:
                yield from _walk_stmts(s.else_body)
        elif isinstance(s, ast.While):
            yield from _walk_stmts(s.body)


def _reachable_from_entry(blocks: list[Block]) -> set[str]:
    succ = {b.name: b.successors() for b in blocks}
    seen: set[str] = set()
    stack = [blocks[0].name]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(succ.get(cur, []))
    return seen


def build_ir(program: ast.Program) -> FunctionIR:
    return IRBuilder(program).build()
