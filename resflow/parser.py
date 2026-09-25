"""Recursive-descent parser for ResFlow.

Grammar (informal; see ``docs/LANGUAGE.md`` for the authoritative version)::

    program   := fn+
    fn        := 'fn' IDENT '(' params? ')' block
    block     := '{' stmt* '}'
    stmt      := let | acquire | release | use | return | throw
               | if | while | try | assign | ';'
    let       := 'let' IDENT ('=' expr)? ';'
    acquire   := 'acquire' IDENT ';'
    release   := 'release' IDENT ';'
    use       := 'use' IDENT ';'
    return    := 'return' expr? ';'
    throw     := 'throw' STRING ';'
    if        := 'if' '(' expr ')' block ('else' block)?
    while     := 'while' '(' expr ')' block
    try       := 'try' block 'catch' '(' IDENT ')' block
    assign    := IDENT '=' expr ';'
    expr      := logic-or
"""

from .errors import ResFlowError
from .lexer import Lexer
from . import ast_nodes as ast


class ParseError(ResFlowError):
    pass


# Spelling -> token kind for single-character punctuation.
_PUNCT_KIND = {"(": "LPAREN", ")": "RPAREN", "{": "LBRACE",
               "}": "RBRACE", ";": "SEMI", ",": "COMMA"}
# Operators keep their spelling as the token kind.
_OPERATOR_KINDS = {"=", "==", "!", "!=", "<", "<=", ">", ">=",
                   "&&", "||", "+", "-", "*", "/", "%"}


class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.i = 0

    # -- token helpers -----------------------------------------------------

    @property
    def tok(self):
        return self.tokens[self.i]

    def _advance(self):
        t = self.tok
        if t.kind != "EOF":
            self.i += 1
        return t

    def _expect(self, kind, value=None):
        t = self.tok
        expected_kind = _PUNCT_KIND.get(kind, kind)
        if t.kind != expected_kind or (value is not None and t.value != value):
            wanted = value if value is not None else kind
            raise ParseError(f"expected {wanted!r}, got {t.value!r}", t.loc)
        return self._advance()

    def _accept(self, kind, value=None):
        t = self.tok
        expected_kind = _PUNCT_KIND.get(kind, kind)
        if t.kind == expected_kind and (value is None or t.value == value):
            return self._advance()
        return None

    # -- program -----------------------------------------------------------

    def parse_program(self):
        funcs = []
        seen = set()
        while self.tok.kind != "EOF":
            fn = self._parse_fn()
            if fn.name in seen:
                raise ParseError(f"duplicate function {fn.name!r}", fn.loc)
            seen.add(fn.name)
            funcs.append(fn)
        if not funcs:
            raise ParseError("program must contain at least one function",
                             self.tok.loc)
        return funcs

    def _parse_fn(self):
        loc = self._expect("KEYWORD", "fn").loc
        name_tok = self._expect("IDENT")
        self._expect("(")
        params = []
        if self.tok.kind != "RPAREN":
            while True:
                p = self._expect("IDENT")
                if p.value in params:
                    raise ParseError(
                        f"duplicate parameter {p.value!r}", p.loc)
                params.append(p.value)
                if not self._accept(","):
                    break
        self._expect(")")
        body, close_loc = self._parse_block()
        return ast.Function(name_tok.value, params, body, loc, close_loc)

    def _parse_block(self):
        self._expect("{")
        stmts = []
        while self.tok.kind not in ("RBRACE", "EOF"):
            stmts.append(self._parse_stmt())
        close_tok = self._expect("}")
        return stmts, close_tok.loc

    # -- statements --------------------------------------------------------

    def _parse_stmt(self):
        t = self.tok
        if t.kind == "SEMI":
            self._advance()
            return self._parse_stmt()
        if t.kind == "KEYWORD":
            handler = {
                "let": self._parse_let,
                "acquire": self._parse_resource_stmt,
                "release": self._parse_resource_stmt,
                "use": self._parse_resource_stmt,
                "return": self._parse_return,
                "throw": self._parse_throw,
                "if": self._parse_if,
                "while": self._parse_while,
                "try": self._parse_try,
            }.get(t.value)
            if handler:
                return handler()
        if t.kind == "IDENT":
            return self._parse_assign()
        raise ParseError(f"unexpected token {t.value!r}", t.loc)

    def _parse_let(self):
        loc = self._advance().loc
        name = self._expect("IDENT").value
        init = None
        if self._accept("="):
            init = self._parse_expr()
        self._expect(";")
        return ast.VarDecl(loc, name, init)

    def _parse_resource_stmt(self):
        kw = self._advance()  # acquire | release | use
        # Both ``acquire(r);`` and ``acquire r;`` styles are accepted.
        if self._accept("("):
            name_tok = self._expect("IDENT")
            self._expect(")")
        else:
            name_tok = self._expect("IDENT")
        self._expect(";")
        cls = {"acquire": ast.Acquire,
               "release": ast.Release,
               "use": ast.Use}[kw.value]
        node = cls(kw.loc, name_tok.value)
        node.name_loc = name_tok.loc
        return node

    def _parse_return(self):
        loc = self._advance().loc
        value = None
        if self.tok.kind != "SEMI":
            value = self._parse_expr()
        self._expect(";")
        return ast.ReturnStmt(loc, value)

    def _parse_throw(self):
        loc = self._advance().loc
        msg_tok = self._expect("STRING")
        self._expect(";")
        return ast.ThrowStmt(loc, msg_tok.value)

    def _parse_if(self):
        loc = self._advance().loc
        self._expect("(")
        cond = self._parse_expr()
        self._expect(")")
        then_body, _ = self._parse_block()
        else_body = []
        if self._accept("KEYWORD", "else"):
            else_body, _ = self._parse_block()
        return ast.IfStmt(loc, cond, then_body, else_body)

    def _parse_while(self):
        loc = self._advance().loc
        self._expect("(")
        cond = self._parse_expr()
        self._expect(")")
        body, _ = self._parse_block()
        return ast.WhileStmt(loc, cond, body)

    def _parse_try(self):
        loc = self._advance().loc
        body, _ = self._parse_block()
        catch_tok = self._expect("KEYWORD", "catch")
        self._expect("(")
        msg = self._expect("IDENT").value
        self._expect(")")
        handler, _ = self._parse_block()
        return ast.TryStmt(loc, body, msg, handler, catch_tok.loc)

    def _parse_assign(self):
        target = self._expect("IDENT")
        self._expect("=")
        value = self._parse_expr()
        self._expect(";")
        return ast.Assign(target.loc, target.value, value)

    # -- expressions (precedence climbing) ---------------------------------

    _BINARY_PRECEDENCE = {
        "||": (1, "left"),
        "&&": (2, "left"),
        "==": (3, "left"), "!=": (3, "left"),
        "<": (4, "left"), "<=": (4, "left"),
        ">": (4, "left"), ">=": (4, "left"),
        "+": (5, "left"), "-": (5, "left"),
        "*": (6, "left"), "/": (6, "left"), "%": (6, "left"),
    }

    def _parse_expr(self):
        return self._parse_binary(0)

    def _parse_binary(self, min_prec):
        left = self._parse_unary()
        while True:
            t = self.tok
            if t.kind not in self._BINARY_PRECEDENCE:
                break
            prec, _ = self._BINARY_PRECEDENCE[t.kind]
            if prec < min_prec:
                break
            op = self._advance().value
            right = self._parse_binary(prec + 1)
            left = ast.Binary(t.loc, op, left, right)
        return left

    def _parse_unary(self):
        t = self.tok
        if t.kind in ("!", "-"):
            self._advance()
            return ast.Unary(t.loc, t.value, self._parse_unary())
        return self._parse_primary()

    def _parse_primary(self):
        t = self.tok
        if t.kind == "INT":
            self._advance()
            return ast.IntLit(t.loc, t.value)
        if t.kind == "BOOL":
            self._advance()
            return ast.BoolLit(t.loc, t.value == "true")
        if t.kind == "STRING":
            self._advance()
            return ast.StrLit(t.loc, t.value)
        if t.kind == "IDENT":
            self._advance()
            return ast.VarRef(t.loc, t.value)
        if self._accept("("):
            e = self._parse_expr()
            self._expect(")")
            return e
        raise ParseError(f"expected expression, got {t.value!r}", t.loc)


def parse_source(source):
    """Lex and parse ``source``; return a list of :class:`ast.Function`."""
    tokens = Lexer(source).tokenize()
    return Parser(tokens).parse_program()
