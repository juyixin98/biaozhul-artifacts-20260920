"""递归下降语法分析（手写，核心分析不依赖任何编译器/解析库）。

文法（EBNF）::

    alternation := concat ('|' concat)*
    concat      := quantified*
    quantified  := atom quantifier?
    quantifier  := '*' | '+' | '?' | '{' n '}' | '{' n ',' '}' | '{' n ',' m '}'
    atom        := literal | '.' | '^' | '$' | '(' alternation ')'

空分支（如 ``a|``、``()``）产生 :class:`Empty`。
``*`` ``+`` ``?`` 在 AST 中统一归一化为 :class:`Repeat`。
"""

from . import ast_nodes as ast
from .errors import RegexSyntaxError
from .lexer import (
    T_ANCHOR_END,
    T_ANCHOR_START,
    T_DOT,
    T_EOF,
    T_LITERAL,
    T_LPAREN,
    T_PIPE,
    T_PLUS,
    T_QUESTION,
    T_REPEAT,
    T_RPAREN,
    T_STAR,
    Token,
)
from .source import Source, Span

_QUANTIFIERS = {T_STAR, T_PLUS, T_QUESTION, T_REPEAT}
_ATOM_STARTERS = {T_LITERAL, T_DOT, T_ANCHOR_START, T_ANCHOR_END, T_LPAREN}


class Parser:
    def __init__(self, source: Source, tokens: list[Token]):
        self.src = source
        self.toks = tokens
        self.i = 0

    # ---- token 游标 ----

    def _peek(self, ahead: int = 0) -> Token:
        j = self.i + ahead
        if j < len(self.toks):
            return self.toks[j]
        return self.toks[-1]

    def _advance(self) -> Token:
        tok = self.toks[self.i]
        if tok.kind != T_EOF:
            self.i += 1
        return tok

    def _error(self, tok: Token, msg: str) -> RegexSyntaxError:
        return RegexSyntaxError(msg, tok.span, self.src)

    # ---- 入口 ----

    def parse(self) -> ast.Node:
        node = self._parse_alternation()
        tok = self._peek()
        if tok.kind == T_RPAREN:
            raise self._error(tok, "多余的右括号 ')'")
        if tok.kind != T_EOF:  # 理论上词法层已覆盖，这里兜底
            raise self._error(tok, f"无法解析的 token: {tok.kind}")
        return node

    # ---- 各语法层 ----

    def _parse_alternation(self) -> ast.Node:
        left = self._parse_concat()
        while self._peek().kind == T_PIPE:
            pipe = self._advance()
            right = self._parse_concat()
            span = self.src.span(left.span.start, right.span.end)
            left = ast.Alt(left, right, span)
        return left

    def _parse_concat(self) -> ast.Node:
        children: list[ast.Node] = []
        while True:
            tok = self._peek()
            if tok.kind in (T_PIPE, T_RPAREN, T_EOF):
                break
            if tok.kind in _QUANTIFIERS:
                label = {
                    T_STAR: "'*'",
                    T_PLUS: "'+'",
                    T_QUESTION: "'?'",
                }.get(tok.kind, f"重复量词 {tok.raw}")
                raise self._error(tok, f"{label} 前面没有可重复的原子")
            atom = self._parse_atom()
            # 一个原子最多直接带一个量词；a** 这种叠加需显式括号 (a*)+
            nxt = self._peek()
            if nxt.kind in _QUANTIFIERS:
                qtok = self._advance()
                atom = self._wrap_quantifier(atom, qtok)
                if self._peek().kind in _QUANTIFIERS:
                    q2 = self._peek()
                    raise self._error(
                        q2, "量词不能直接叠加；如需对重复再重复请加括号，例如 (a*)+"
                    )
            children.append(atom)
        if not children:
            pos = self._peek().span.start
            return ast.Empty(self.src.span(pos, pos))
        if len(children) == 1:
            return children[0]
        span = self.src.span(children[0].span.start, children[-1].span.end)
        return ast.Concat(children, span)

    def _wrap_quantifier(self, atom: ast.Node, qtok: Token) -> ast.Repeat:
        if qtok.kind == T_STAR:
            mn, mx = 0, None
        elif qtok.kind == T_PLUS:
            mn, mx = 1, None
        elif qtok.kind == T_QUESTION:
            mn, mx = 0, 1
        else:  # T_REPEAT
            mn, mx = qtok.value  # type: ignore[misc]
        span = self.src.span(atom.span.start, qtok.span.end)
        return ast.Repeat(atom, mn, mx, span)

    def _parse_atom(self) -> ast.Node:
        tok = self._peek()
        if tok.kind == T_LITERAL:
            self._advance()
            return ast.Literal(tok.codepoint, tok.span)
        if tok.kind == T_DOT:
            self._advance()
            return ast.AnyChar(tok.span)
        if tok.kind == T_ANCHOR_START:
            self._advance()
            return ast.Anchor("^", tok.span)
        if tok.kind == T_ANCHOR_END:
            self._advance()
            return ast.Anchor("$", tok.span)
        if tok.kind == T_LPAREN:
            return self._parse_group()
        # 兜底（正常情况下 _parse_concat 已拦截量词/分隔符）
        raise self._error(tok, f"此处应为原子，但遇到 {tok.kind}")

    def _parse_group(self) -> ast.Node:
        lparen = self._advance()  # '('
        inner = self._parse_alternation()
        rparen = self._peek()
        if rparen.kind != T_RPAREN:
            raise self._error(lparen, "括号 '(' 没有对应的 ')'")
        self._advance()
        return ast.Group(inner, self.src.span(lparen.span.start, rparen.span.end))


def parse(source: Source, tokens: list[Token]) -> ast.Node:
    return Parser(source, tokens).parse()
