"""Lower the parsed AST into the non-SSA integer IR.

Conventions
-----------
* Source variables are *slots*: reads become ``load``, writes ``store``.
* Function parameters seed their slot with ``%pN`` (no explicit store in
  the entry block; the SSA renamer seeds its value stack the same way).
* Expressions are always materialised to an SSA-shaped temporary; integer
  constants are cached per function.
* ``&&`` / ``||`` are lowered to control flow through a compiler-generated
  internal slot (``__scN__``), preserving short-circuit semantics.
* ``if``/``while`` always emit both sides structurally; when the condition
  folds to a constant the branch still uses that constant, leaving the
  unchosen side as a genuinely *unreachable block* (which the SSA pipeline
  reports and prunes).
* Statements following a ``return`` in the same statement list are dropped
  as unreachable.
"""
from __future__ import annotations

from .. import ast_nodes as ast
from ..errors import LoweringError
from ..ir import (BIN_OPS, Block, Function, Instr, Module,
                  make_br, make_jmp, make_ret)


class FuncBuilder:
    def __init__(self, func: ast.Func, signatures: dict[str, list[str]]):
        self.ast = func
        self.sigs = signatures
        self.fn = Function(name=func.name,
                           params=[f"%p{i}" for i in range(len(func.params))],
                           entry="entry", blocks={})
        self.fn.loc = func.loc
        entry = Block("entry", loc=func.loc)
        self.fn.add_block(entry)
        self.cur = "entry"
        self.temp_n = 0
        self.const_n = 0
        self.const_insert_at = 0
        self.const_cache: dict[int, str] = {}
        self.sc_n = 0
        self.if_n = 0
        self.while_n = 0
        self.declared: dict[str, ast.Loc] = {}
        for i, p in enumerate(func.params):
            self.declared[p] = func.loc
        self.dropped_unreachable = 0

    # ------------------------------------------------------------- utilities

    @property
    def block(self) -> Block:
        return self.fn.blocks[self.cur]

    def _new_block(self, name: str, loc=None) -> str:
        label = self.fn.new_block_name(name)
        self.fn.add_block(Block(label, loc=loc))
        return label

    def _fresh_temp(self) -> str:
        name = f"%t{self.temp_n}"
        self.temp_n += 1
        return name

    def _const(self, value: int, loc=None) -> str:
        # Constants are pure: cache them per function and keep every
        # materialised constant at the top of the entry block so the value
        # dominates every use (uses may sit in different branches).
        if value in self.const_cache:
            return self.const_cache[value]
        name = f"%c{self.const_n}"
        self.const_n += 1
        entry = self.fn.blocks[self.fn.entry]
        entry.instrs.insert(self.const_insert_at,
                            Instr("const", name, [], {"value": value}, loc))
        self.const_insert_at += 1
        self.const_cache[value] = name
        return name

    # ----------------------------------------------------------------- body

    def build(self) -> Function:
        self.emit_stmts(self.ast.body)
        if self.block.term is None:
            self.block.term = make_ret(self._const(0))
        return self.fn

    def emit_stmts(self, stmts) -> None:
        for s in stmts:
            if self.block.term is not None:
                # Statements after a terminator in the same syntactic list
                # are unreachable (e.g. code after return).
                self.dropped_unreachable += 1
                continue
            self.emit_stmt(s)

    def emit_stmt(self, s) -> None:
        if isinstance(s, ast.VarDecl):
            if s.name in self.declared:
                raise LoweringError(f"variable {s.name!r} already declared", s.loc)
            if s.name.startswith("__"):
                raise LoweringError("identifiers starting with '__' are reserved", s.loc)
            self.declared[s.name] = s.loc
            val = self.emit_expr(s.init)
            self.block.instrs.append(
                Instr("store", None, [val], {"var": s.name}, s.loc))
        elif isinstance(s, ast.Assign):
            if s.name not in self.declared:
                raise LoweringError(f"assignment to undeclared variable {s.name!r}",
                                    s.loc)
            val = self.emit_expr(s.value)
            self.block.instrs.append(
                Instr("store", None, [val], {"var": s.name}, s.loc))
        elif isinstance(s, ast.ExprStmt):
            self.emit_expr(s.expr)
        elif isinstance(s, ast.Return):
            val = self.emit_expr(s.value) if s.value is not None else None
            self.block.term = make_ret(val, s.loc)
        elif isinstance(s, ast.Block):
            self.emit_stmts(s.stmts)
        elif isinstance(s, ast.If):
            self.emit_if(s)
        elif isinstance(s, ast.While):
            self.emit_while(s)
        else:  # pragma: no cover - defensive
            raise LoweringError(f"cannot lower statement {type(s).__name__}", s.loc)

    # ------------------------------------------------------------ control flow

    def emit_if(self, s: ast.If) -> None:
        self.if_n += 1
        base = f"b.if{self.if_n}"
        then_l = self._new_block(f"{base}.then", s.loc)
        else_l = self._new_block(f"{base}.else", s.loc)
        merge_l = self._new_block(f"{base}.merge", s.loc)

        folded = fold_const(s.cond)
        if folded is None:
            cond = self.emit_expr(s.cond)
            self.block.term = make_br(cond, then_l, else_l, loc=s.loc)
            active_then, active_else = then_l, else_l
        else:
            # Static condition: only jump to the taken side.  The other
            # block remains structurally present (and gets built below)
            # but has no CFG predecessor, so dominance analysis reports it
            # as unreachable and the SSA pipeline prunes it.
            taken = then_l if folded != 0 else else_l
            self.block.term = make_jmp(taken, loc=s.loc)

        self.cur = then_l
        self.emit_stmts(s.then_body)
        if self.block.term is None:
            self.block.term = make_jmp(merge_l)

        self.cur = else_l
        self.emit_stmts(s.else_body)
        if self.block.term is None:
            self.block.term = make_jmp(merge_l)

        self.cur = merge_l

    def emit_while(self, s: ast.While) -> None:
        self.while_n += 1
        base = f"b.while{self.while_n}"
        cond_l = self._new_block(f"{base}.cond", s.loc)
        body_l = self._new_block(f"{base}.body", s.loc)
        end_l = self._new_block(f"{base}.end", s.loc)

        folded = fold_const(s.cond)
        if folded == 0:
            # Never executes: jump straight to end; cond and body blocks
            # stay in the function unreachable.
            self.block.term = make_jmp(end_l, loc=s.loc)
            self.cur = cond_l
            self.emit_expr(s.cond)
            self.block.term = make_br(self._const(0), body_l, end_l, loc=s.loc)
            self.cur = body_l
            self.emit_stmts(s.body)
            if self.block.term is None:
                self.block.term = make_jmp(cond_l)
            self.cur = end_l
            return

        self.block.term = make_jmp(cond_l)

        self.cur = cond_l
        if folded is not None:
            self.emit_expr(s.cond)
            cond = self._const(folded)
        else:
            cond = self.emit_expr(s.cond)
        self.block.term = make_br(cond, body_l, end_l, loc=s.loc)

        self.cur = body_l
        self.emit_stmts(s.body)
        if self.block.term is None:
            self.block.term = make_jmp(cond_l)

        self.cur = end_l

    # ------------------------------------------------------------ expressions

    def emit_expr(self, e) -> str:
        if isinstance(e, ast.IntLit):
            return self._const(int(e.value), e.loc)
        if isinstance(e, ast.BoolLit):
            return self._const(1 if e.value else 0, e.loc)
        if isinstance(e, ast.Name):
            if e.name not in self.declared:
                raise LoweringError(f"use of undeclared variable {e.name!r}", e.loc)
            dest = self._fresh_temp()
            self.block.instrs.append(
                Instr("load", dest, [], {"var": e.name}, e.loc))
            return dest
        if isinstance(e, ast.Unary):
            return self.emit_unary(e)
        if isinstance(e, ast.Binary):
            if e.op in ("&&", "||"):
                return self.emit_logical(e)
            l = self.emit_expr(e.left)
            r = self.emit_expr(e.right)
            if e.op not in BIN_OPS:  # pragma: no cover - parser guarantees
                raise LoweringError(f"unsupported operator {e.op!r}", e.loc)
            dest = self._fresh_temp()
            self.block.instrs.append(
                Instr("binop", dest, [l, r], {"binop": e.op}, e.loc))
            return dest
        if isinstance(e, ast.Call):
            return self.emit_call(e)
        raise LoweringError(f"cannot lower expression {type(e).__name__}", e.loc)  # pragma: no cover

    def emit_unary(self, e: ast.Unary) -> str:
        x = self.emit_expr(e.arg)
        if e.op == "-":
            other = self._const(0)
            op = "-"
        elif e.op == "!":
            other = self._const(0)
            op = "=="
        elif e.op == "~":
            other = self._const(-1)
            op = "^"
        else:  # pragma: no cover
            raise LoweringError(f"unsupported unary operator {e.op!r}", e.loc)
        dest = self._fresh_temp()
        self.block.instrs.append(
            Instr("binop", dest, [other, x], {"binop": op}, e.loc))
        return dest

    def emit_logical(self, e: ast.Binary) -> str:
        """Short-circuit && / || via an internal slot."""
        self.sc_n += 1
        slot = f"__sc{self.sc_n}__"
        right_l = self._new_block(f"b.sc{self.sc_n}.right")
        short_l = self._new_block(f"b.sc{self.sc_n}.short")
        cont_l = self._new_block(f"b.sc{self.sc_n}.cont")
        is_and = e.op == "&&"

        left = self.emit_expr(e.left)
        self.block.term = make_br(left,
                                  right_l if is_and else short_l,
                                  short_l if is_and else right_l)

        self.cur = right_l
        r = self.emit_expr(e.right)
        self.block.instrs.append(Instr("store", None, [r], {"var": slot}))
        self.block.term = make_jmp(cont_l)

        self.cur = short_l
        short_val = self._const(0 if is_and else 1)
        self.block.instrs.append(
            Instr("store", None, [short_val], {"var": slot}))
        self.block.term = make_jmp(cont_l)

        self.cur = cont_l
        dest = self._fresh_temp()
        self.block.instrs.append(Instr("load", dest, [], {"var": slot}, e.loc))
        return dest

    def emit_call(self, e: ast.Call) -> str:
        args = [self.emit_expr(a) for a in e.args]
        if e.name == "print":
            if len(args) != 1:
                raise LoweringError("print() expects exactly one argument", e.loc)
            self.block.instrs.append(Instr("print", None, args, loc=e.loc))
            return self._const(0, e.loc)
        if e.name not in self.sigs:
            raise LoweringError(f"call to unknown function {e.name!r}()", e.loc)
        if len(args) != len(self.sigs[e.name]):
            raise LoweringError(
                f"function {e.name!r} expects {len(self.sigs[e.name])} "
                f"arguments but got {len(args)}", e.loc)
        dest = self._fresh_temp()
        self.block.instrs.append(
            Instr("call", dest, args, {"name": e.name}, e.loc))
        return dest


# ------------------------------------------------------------- const folding

def fold_const(e, params_const: set[str] | None = None):
    """Constant evaluate a pure expression; return int or None."""
    if isinstance(e, ast.IntLit):
        return int(e.value)
    if isinstance(e, ast.BoolLit):
        return 1 if e.value else 0
    if isinstance(e, ast.Unary):
        v = fold_const(e.arg)
        if v is None:
            return None
        if e.op == "-":
            return -v
        if e.op == "!":
            return 1 if v == 0 else 0
        if e.op == "~":
            return ~v
    if isinstance(e, ast.Binary) and e.op not in ("&&", "||"):
        a = fold_const(e.left)
        b = fold_const(e.right)
        if a is None or b is None:
            return None
        try:
            return apply_binop(e.op, a, b)
        except ZeroDivisionError:
            return None
    return None


def apply_binop(op: str, a: int, b: int) -> int:
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        q = abs(a) // abs(b)
        return q if (a < 0) == (b < 0) else -q
    if op == "%":
        q = abs(a) // abs(b)
        rem = abs(a) - q * abs(b)
        return rem if a >= 0 else -rem
    if op == "==":
        return int(a == b)
    if op == "!=":
        return int(a != b)
    if op == "<":
        return int(a < b)
    if op == "<=":
        return int(a <= b)
    if op == ">":
        return int(a > b)
    if op == ">=":
        return int(a >= b)
    if op == "&":
        return a & b
    if op == "|":
        return a | b
    if op == "^":
        return a ^ b
    if op == "<<":
        return a << b
    if op == ">>":
        return a >> b
    raise ValueError(op)  # pragma: no cover


def build_module(program: ast.Program) -> tuple[Module, dict]:
    signatures = {f.name: list(f.params) for f in program.funcs}
    if len(signatures) != len(program.funcs):
        dupes = sorted({f.name for f in program.funcs
                        if sum(x.name == f.name for x in program.funcs) > 1})
        raise LoweringError(f"duplicate function definitions: {dupes}")
    m = Module()
    dropped = {}
    for f in program.funcs:
        fb = FuncBuilder(f, signatures)
        m.add_function(fb.build())
        if fb.dropped_unreachable:
            dropped[f.name] = fb.dropped_unreachable
    return m, {"dropped_statements": dropped}
