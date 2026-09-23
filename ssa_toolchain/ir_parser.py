"""Parser for the IR text format produced by :func:`ssa_toolchain.ir.dump_function`.

The format::

    func name(%p0, %p1) {
    entry:
      %t = const 3
      %t2 = add %t, %one          ; operands are SSA names
      %v  = load x
      store %v -> x
      %m  = phi [pred %a] [pred2 %b]
      jmp loop(%v, %zero)
      br %c, then(%v), else(%one)
      ret %v
    }

It round-trips pre-SSA IR (loads/stores, no phis), SSA IR (phis + edge
arguments) and phi-free executable IR.  Integer literals appearing where an
operand is expected are accepted as a convenience and materialised into
fresh ``const`` instructions inside the block that uses them (for phi
incoming values they must already have been materialised in the predecessor,
as the real pipeline always does).
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import IRError, Loc
from .ir import (BIN_OPS, Block, Function, Instr, Module, make_br, make_jmp,
                 make_ret, Phi)


@dataclass
class T:
    kind: str
    text: str
    loc: Loc


_TERM_KW = {"jmp", "br", "ret"}
_INSTR_KW = {"const", "copy", "load", "store", "call", "print"}
_IR_SINGLE = set("(){},:%=+-*/<>!&|^[]")


def _tokenize(src: str, file: str) -> list[T]:
    toks: list[T] = []
    i, n = 0, len(src)
    line, col = 1, 1

    def loc_of(start, sl, sc, end_off, el=None, ec=None):
        return Loc(file, start, end_off, sl, sc, el or sl, ec or sc)

    def advance():
        nonlocal i, line, col
        if src[i] == "\n":
            i += 1
            line += 1
            col = 1
        else:
            i += 1
            col += 1

    while i < n:
        c = src[i]
        if c in " \t\r\n":
            advance()
            continue
        if c == ";":
            while i < n and src[i] != "\n":
                advance()
            continue
        start, sl, sc = i, line, col
        if c == "%":
            advance()
            while i < n and (src[i].isalnum() or src[i] in "_."):
                advance()
            if i == start + 1:
                raise IRError("expected value name after '%'",
                              loc_of(start, sl, sc, i, line, col))
            toks.append(T("value", src[start:i], loc_of(start, sl, sc, i, line, col)))
            continue
        if c.isalpha() or c == "_":
            while i < n and (src[i].isalnum() or src[i] in "_."):
                advance()
            toks.append(T("ident", src[start:i], loc_of(start, sl, sc, i, line, col)))
            continue
        if c.isdigit() or (c == "-" and i + 1 < n and src[i + 1].isdigit()
                           and not _is_arrow(src, i)):
            advance()
            while i < n and src[i].isdigit():
                advance()
            toks.append(T("int", src[start:i], loc_of(start, sl, sc, i, line, col)))
            continue
        matched = False
        for op in ("==", "!=", "<=", ">=", "<<", ">>", "->"):
            if src.startswith(op, i):
                for _ in op:
                    advance()
                toks.append(T(op, op, loc_of(start, sl, sc, i, line, col)))
                matched = True
                break
        if matched:
            continue
        if c in _IR_SINGLE:
            advance()
            toks.append(T(c, c, loc_of(start, sl, sc, i, line, col)))
            continue
        raise IRError(f"unexpected character {c!r} in IR",
                      loc_of(start, sl, sc, i + 1, line, col))
    toks.append(T("eof", "", Loc(file, n, n, line, col, line, col)))
    return toks


def _is_arrow(src: str, i: int) -> bool:
    return i + 1 < len(src) and src[i:i + 2] == "->"


class _IRParser:
    def __init__(self, src: str, file: str):
        self.toks = _tokenize(src, file)
        self.pos = 0
        self.const_counter = 0
        self.used_names: set[str] = set()

    @property
    def cur(self) -> T:
        return self.toks[self.pos]

    def ahead(self, k: int) -> T:
        return self.toks[min(self.pos + k, len(self.toks) - 1)]

    def eat(self) -> T:
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def accept(self, kind: str) -> T | None:
        if self.cur.kind == kind:
            return self.eat()
        return None

    def expect(self, kind: str) -> T:
        if self.cur.kind != kind:
            raise IRError(f"expected {kind!r} in IR but found {self.cur.text!r}",
                          self.cur.loc)
        return self.eat()

    # ---------------------------------------------------------------- module

    def parse_module(self) -> Module:
        m = Module()
        while self.cur.kind != "eof":
            m.add_function(self.parse_func())
        return m

    def parse_func(self) -> Function:
        kw = self.expect("ident")
        if kw.text != "func":
            raise IRError(f"expected 'func' but found {kw.text!r}", kw.loc)
        name_tok = self.expect("ident")
        self.expect("(")
        params: list[str] = []
        if self.cur.kind != ")":
            while True:
                params.append(self.expect("value").text)
                if not self.accept(","):
                    break
        self.expect(")")
        self.expect("{")
        f = Function(name=name_tok.text, params=params, entry="", blocks={})
        while self.cur.kind != "}":
            if self.cur.kind == "eof":
                raise IRError("unexpected end of input inside function", self.cur.loc)
            label = self.expect("ident").text
            self.expect(":")
            if not f.ordered_labels:
                f.entry = label
            b = Block(label)
            f.add_block(b)
            self.parse_block(b)
        self.expect("}")
        return f

    # ----------------------------------------------------------------- block

    def _at_label(self) -> bool:
        return self.cur.kind == "ident" and self.ahead(1).kind == ":"

    def parse_block(self, b: Block) -> None:
        # phi nodes: they look like value assignments whose RHS keyword is
        # exactly 'phi', and they only occur at the top of a block.
        while (self.cur.kind == "value" and self.ahead(1).kind == "="
               and self.ahead(2).kind == "ident"
               and self.ahead(2).text == "phi"):
            dest = self.eat().text
            self.eat()
            kw = self.eat()
            incoming: list[tuple[str, str]] = []
            while self.cur.kind == "[":
                self.eat()
                pred = self.expect("ident").text
                val = self.parse_phi_operand()
                self.expect("]")
                incoming.append((pred, val))
            if not incoming:
                raise IRError("phi needs at least one incoming value", kw.loc)
            var = dest.lstrip("%").split(".phi")[0]
            b.phis.append(Phi(dest=dest, var=var, incoming=incoming))

        # body instructions
        while True:
            if self.cur.kind == "ident" and self.cur.text in _TERM_KW:
                b.term = self.parse_term(b)
                return
            if self.cur.kind == "}" or self._at_label():
                return
            if self.cur.kind == "eof":
                raise IRError("unterminated block", self.cur.loc)
            self.parse_instruction(b)
    def parse_instruction(self, b: Block) -> None:
        if self.cur.kind == "value" and self.ahead(1).kind == "=":
            dest = self.eat().text
            self.eat()
            b.instrs.append(self.parse_rhs(b, dest))
            return
        if self.cur.kind == "ident" and self.cur.text == "store":
            self.eat()
            val = self.operand(b)
            self.expect("->")
            var = self.expect("ident").text
            b.instrs.append(Instr("store", None, [val], {"var": var}))
            return
        if self.cur.kind == "ident" and self.cur.text == "print":
            self.eat()
            val = self.operand(b)
            b.instrs.append(Instr("print", None, [val]))
            return
        raise IRError(f"cannot parse instruction starting with {self.cur.text!r}",
                      self.cur.loc)

    def parse_rhs(self, b: Block, dest: str) -> Instr:
        head = self.eat()
        kind = head.kind if head.kind != "ident" else head.text
        if kind == "const":
            val = self.expect("int")
            return Instr("const", dest, [], {"value": int(val.text)})
        if kind == "copy":
            return Instr("copy", dest, [self.operand(b)])
        if kind == "load":
            var = self.expect("ident").text
            return Instr("load", dest, [], {"var": var})
        if kind == "call":
            callee = self.expect("ident").text
            return Instr("call", dest, self.parse_arg_list(b), {"name": callee})
        if kind in BIN_OPS:
            left = self.operand(b)
            self.expect(",")
            right = self.operand(b)
            return Instr("binop", dest, [left, right], {"binop": kind})
        raise IRError(f"unknown instruction kind {kind!r}", head.loc)

    def parse_term(self, b: Block):
        kw = self.eat()
        if kw.text == "ret":
            if self.cur.kind in ("value", "int"):
                return make_ret(self.operand(b))
            return make_ret(None)
        if kw.text == "jmp":
            target, args = self.parse_edge(b)
            return make_jmp(target, args)
        if kw.text == "br":
            cond = self.operand(b)
            self.expect(",")
            t, at = self.parse_edge(b)
            self.expect(",")
            fl, af = self.parse_edge(b)
            return make_br(cond, t, fl, at, af)
        raise IRError(f"unknown terminator {kw.text!r}", kw.loc)

    def parse_edge(self, b: Block) -> tuple[str, list[str]]:
        label = self.expect("ident").text
        args: list[str] = []
        if self.accept("("):
            if self.cur.kind != ")":
                while True:
                    args.append(self.operand(b))
                    if not self.accept(","):
                        break
            self.expect(")")
        return label, args

    def parse_arg_list(self, b: Block) -> list[str]:
        self.expect("(")
        args: list[str] = []
        if self.cur.kind != ")":
            while True:
                args.append(self.operand(b))
                if not self.accept(","):
                    break
        self.expect(")")
        return args

    # -------------------------------------------------------------- operands

    def parse_phi_operand(self) -> str:
        # Phi incoming values must be SSA names materialised in predecessor
        # blocks - an inline literal here would have no defining block.
        if self.cur.kind == "int":
            raise IRError(
                "integer literal in phi incoming list is ambiguous; "
                "materialise it in the predecessor first",
                self.cur.loc,
            )
        return self.expect("value").text

    def operand(self, b: Block) -> str:
        if self.cur.kind == "value":
            return self.eat().text
        if self.cur.kind == "int":
            tok = self.eat()
            name = self._fresh_const_name()
            b.instrs.append(Instr("const", name, [], {"value": int(tok.text)}))
            return name
        raise IRError(f"expected operand but found {self.cur.text!r}", self.cur.loc)

    def _fresh_const_name(self) -> str:
        i = self.const_counter
        while True:
            name = f"%__lit{i}"
            i += 1
            if name not in self.used_names:
                self.const_counter = i
                self.used_names.add(name)
                return name


def parse_ir(src: str, file: str = "<ir>") -> Module:
    return _IRParser(src, file).parse_module()
