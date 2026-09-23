"""Hand-written recursive-descent parser: token stream -> AST.

No compiler toolkit is used; this is the project's own parser.
The grammar is documented in docs/syntax.md.
"""

from . import ast_nodes as ast
from .errors import ParseError
from .source import Loc, Token
from .lexer import lex

# Token kinds that introduce a declaration.
_DECL_START = {"var", "input", "arr"}


class Parser:
    def __init__(self, tokens, src):
        self.toks = tokens
        self.src = src
        self.pos = 0

    # ------------------------------------------------------------- helpers
    def peek(self, k=0):
        return self.toks[min(self.pos + k, len(self.toks) - 1)]

    def take(self):
        t = self.toks[self.pos]
        if self.pos < len(self.toks) - 1:
            self.pos += 1
        return t

    def at(self, kind):
        return self.peek().kind == kind

    def accept(self, kind):
        if self.at(kind):
            return self.take()
        return None

    def expect(self, kind, what=None):
        t = self.peek()
        if t.kind != kind:
            raise ParseError(
                f"expected {what or kind!r} but found {self._show(t)}", t.loc)
        return self.take()

    @staticmethod
    def _show(t):
        if t.kind == "EOF":
            return "end of file"
        if t.kind in ("ID", "INT"):
            return f"{t.kind.lower()} {t.text!r}"
        return repr(t.kind)

    # ------------------------------------------------------------- program
    def parse_program(self):
        start = self.peek().loc
        var_decls, arr_decls = [], []
        while self.peek().kind in _DECL_START:
            d = self.parse_decl()
            if isinstance(d, ast.ArrDecl):
                arr_decls.append(d)
            else:
                var_decls.append(d)
        body = self.parse_stmt_list_until("EOF")
        loc = Loc.merge(start, self.peek().loc)
        return ast.Program(var_decls, arr_decls, body, loc)

    def parse_decl(self):
        kw = self.take()
        name_tok = self.expect("ID", "identifier")
        if kw.kind == "input":
            self.expect(";")
            return ast.VarDecl(name_tok.text, None, True,
                               Loc.merge(kw.loc, self.toks[self.pos - 1].loc))
        if kw.kind == "var":
            init = None
            if self.accept("="):
                init = self.parse_expr()
            semi = self.expect(";")
            return ast.VarDecl(name_tok.text, init, False,
                               Loc.merge(kw.loc, semi.loc))
        # arr
        self.expect("[")
        size_tok = self.expect("INT", "array size literal")
        self.expect("]")
        size_loc = size_tok.loc
        self.expect("=")
        self.expect("[")
        elems = []
        if not self.at("]"):
            elems.append(self.parse_signed_int())
            while self.accept(","):
                elems.append(self.parse_signed_int())
        self.expect("]")
        semi = self.expect(";")
        return ast.ArrDecl(name_tok.text, int(size_tok.text), elems,
                           Loc.merge(kw.loc, semi.loc), size_loc=size_loc)

    def parse_signed_int(self):
        neg = False
        if self.accept("-"):
            neg = True
        t = self.expect("INT", "integer literal")
        v = int(t.text)
        return -v if neg else v

    # ------------------------------------------------------------ statements
    def parse_stmt_list_until(self, end):
        stmts = []
        while not self.at(end):
            stmts.append(self.parse_stmt())
        if end != "EOF":
            self.take()  # consume closing brace
        return stmts

    def parse_stmt(self):
        t = self.peek()
        if t.kind in _DECL_START:
            raise ParseError("declarations must precede all statements", t.loc)
        if t.kind == "{":
            return self.parse_block()
        if t.kind == "if":
            return self.parse_if()
        if t.kind == "while":
            return self.parse_while()
        # assignment statement
        start = t.loc
        target = self.parse_lvalue()
        self.expect("=")
        value = self.parse_expr()
        semi = self.expect(";")
        return ast.Assign(target, value, Loc.merge(start, semi.loc))

    def parse_block(self):
        # A block is just a statement list; braces remain in the loc span.
        brace = self.take()
        stmts = self.parse_stmt_list_until("}")
        # Blocks flatten into their statement list at AST level.  To keep
        # scopes simple Imp has no block-local declarations, so wrapping is
        # unnecessary; callers see a plain list.  We return a marker node.
        return _Block(stmts, Loc.merge(brace.loc, self.toks[self.pos - 1].loc))

    def parse_if(self):
        kw = self.take()
        self.expect("(")
        cond = self.parse_expr()
        self.expect(")")
        then_body = self.parse_block_body()
        else_body = None
        if self.accept("else"):
            else_body = self.parse_block_body()
        return ast.If(cond, then_body, else_body,
                      Loc.merge(kw.loc, self.toks[self.pos - 1].loc))

    def parse_while(self):
        kw = self.take()
        self.expect("(")
        cond = self.parse_expr()
        self.expect(")")
        body = self.parse_block_body()
        return ast.While(cond, body, Loc.merge(kw.loc, self.toks[self.pos - 1].loc))

    def parse_block_body(self):
        # Bodies require braces, as documented (also avoids the dangling-else
        # ambiguity entirely).
        self.expect("{")
        return self.parse_stmt_list_until("}")

    def parse_lvalue(self):
        name_tok = self.expect("ID", "identifier")
        if self.accept("["):
            idx = self.parse_expr()
            bracket = self.expect("]")
            return ast.ArrayRef(name_tok.text, idx,
                                Loc.merge(name_tok.loc, bracket.loc))
        return ast.Var(name_tok.text, name_tok.loc)

    # ------------------------------------------------------------- expressions
    # Precedence (low -> high): or, and, equality/relational, additive,
    # multiplicative, unary, primary.

    _OR_OPS = {"||", "or"}
    _AND_OPS = {"&&", "and"}
    _CMP_OPS = {"==", "!=", "<", "<=", ">", ">="}
    _ADD_OPS = {"+", "-"}
    _MUL_OPS = {"*", "/", "%"}

    def parse_expr(self):
        return self.parse_or()

    def _parse_binary_level(self, sub, ops):
        start_loc = self.peek().loc
        lhs = sub()
        while self.peek().kind in ops:
            op_tok = self.take()
            rhs = sub()
            lhs = ast.Binary(op_tok.kind, lhs, rhs,
                             Loc.merge(getattr(lhs, "loc", start_loc),
                                       getattr(rhs, "loc", op_tok.loc)))
        return lhs

    def parse_or(self):
        return self._parse_binary_level(self.parse_and, self._OR_OPS)

    def parse_and(self):
        return self._parse_binary_level(self.parse_not, self._AND_OPS)

    def parse_not(self):
        t = self.peek()
        if t.kind in ("not", "!"):
            self.take()
            e = self.parse_not()
            return ast.Unary("not", e, Loc.merge(t.loc, getattr(e, "loc", t.loc)))
        return self.parse_cmp()

    def parse_cmp(self):
        return self._parse_binary_level(self.parse_add, self._CMP_OPS)

    def parse_add(self):
        return self._parse_binary_level(self.parse_mul, self._ADD_OPS)

    def parse_mul(self):
        return self._parse_binary_level(self.parse_unary, self._MUL_OPS)

    def parse_unary(self):
        t = self.peek()
        if t.kind == "-":
            self.take()
            e = self.parse_unary()
            return ast.Unary("-", e, Loc.merge(t.loc, getattr(e, "loc", t.loc)))
        if t.kind == "+":
            self.take()
            return self.parse_unary()
        return self.parse_primary()

    def parse_primary(self):
        t = self.peek()
        if t.kind == "INT":
            self.take()
            return ast.IntLit(int(t.text), t.loc)
        if t.kind in ("true", "false"):
            self.take()
            return ast.BoolLit(t.kind == "true", t.loc)
        if t.kind == "(":
            self.take()
            e = self.parse_expr()
            self.expect(")")
            return e
        if t.kind == "ID":
            self.take()
            if self.accept("["):
                idx = self.parse_expr()
                bracket = self.expect("]")
                return ast.ArrayRef(t.text, idx, Loc.merge(t.loc, bracket.loc))
            return ast.Var(t.text, t.loc)
        raise ParseError(f"unexpected token {self._show(t)} in expression", t.loc)


class _Block:
    """Internal marker produced for bare blocks; flattened by resolve()."""
    __slots__ = ("stmts", "loc")

    def __init__(self, stmts, loc):
        self.stmts = stmts
        self.loc = loc


def parse(src: str) -> ast.Program:
    return Parser(lex(src), src).parse_program()
