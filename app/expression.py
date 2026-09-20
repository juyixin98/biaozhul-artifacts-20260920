"""Restricted expression language for condition edges.

A hand-written tokenizer + recursive-descent parser is used on purpose:
``eval``/``exec`` and attribute access are impossible, so a published template
can never execute arbitrary Python.

Supported grammar (Python-like)::

    expr      := or_expr
    or_expr   := and_expr (or and_expr)*
    and_expr  := not_expr (and not_expr)*
    not_expr  := not not_expr | comparison
    comparison := addition (==|!=|>|>=|<|<=|in addition)?
    addition  := multiplication ((+|-) multiplication)*
    multiplication := unary ((*|/|%) unary)*
    unary     := (-|not) unary | primary
    primary   := NUMBER | STRING | true | false | null
               | NAME ('(' args ')')? | NAME ('.' NAME)*
               | '(' expr ')' | '[' expr (',' expr)* ']'

Values come only from the instance payload (dotted read paths such as
``amount`` or ``form.region``). Whitelisted pure functions: ``len``,
``lower``, ``upper``. Missing variables resolve to ``None``.
"""
from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ExpressionError(ValueError):
    """Raised on any parse or evaluation error in a restricted expression."""


# --------------------------------------------------------------------------- #
# Tokenizer
# --------------------------------------------------------------------------- #

_KEYWORDS = {"and", "or", "not", "in", "true", "false", "null"}


class Token:
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind: str, value: Any, pos: int):
        self.kind = kind
        self.value = value
        self.pos = pos

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Token({self.kind!r}, {self.value!r})"


def _tokenize(src: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            j = i
            seen_dot = False
            while j < n and (src[j].isdigit() or (src[j] == "." and not seen_dot)):
                seen_dot = seen_dot or src[j] == "."
                j += 1
            raw = src[i:j]
            tokens.append(Token("number", float(raw) if "." in raw else int(raw), i))
            i = j
            continue
        if c in ("'", '"'):
            quote = c
            j = i + 1
            buf: list[str] = []
            while j < n and src[j] != quote:
                if src[j] == "\\" and j + 1 < n:
                    esc = src[j + 1]
                    buf.append({"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"'}.get(esc, esc))
                    j += 2
                else:
                    buf.append(src[j])
                    j += 1
            if j >= n:
                raise ExpressionError("unterminated string literal")
            tokens.append(Token("string", "".join(buf), i))
            i = j + 1
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            # Dunder names (__import__, __class__, __globals__, ...) are the
            # usual escape route out of sandboxes; reject them at lex time.
            if word.startswith("__") and word.endswith("__"):
                raise ExpressionError(f"reserved name {word!r} is not allowed")
            if word in ("and", "or", "not", "in"):
                tokens.append(Token(word, word, i))
            elif word == "true":
                tokens.append(Token("bool", True, i))
            elif word == "false":
                tokens.append(Token("bool", False, i))
            elif word == "null" or word == "None":
                tokens.append(Token("null", None, i))
            else:
                tokens.append(Token("name", word, i))
            i = j
            continue
        two = src[i : i + 2]
        if two in ("==", "!=", ">=", "<="):
            tokens.append(Token("op", two, i))
            i += 2
            continue
        if c in "+-*/%()[].,<>=!":
            # Single '=' alone is illegal (no assignment) — caught by parser.
            tokens.append(Token("op", c, i))
            i += 1
            continue
        raise ExpressionError(f"unexpected character {c!r} at position {i}")
    tokens.append(Token("eof", None, n))
    return tokens


# --------------------------------------------------------------------------- #
# Parser -> AST (plain tuples)
# --------------------------------------------------------------------------- #


class _Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def next(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind: str, value: Any | None = None) -> Token:
        tok = self.next()
        if tok.kind != kind or (value is not None and tok.value != value):
            raise ExpressionError(f"expected {value or kind} at position {tok.pos}")
        return tok

    def parse(self) -> tuple:
        node = self.parse_or()
        if self.peek().kind != "eof":
            raise ExpressionError(f"unexpected token at position {self.peek().pos}")
        return node

    def parse_or(self) -> tuple:
        node = self.parse_and()
        while self.peek().kind == "or":
            self.next()
            node = ("or", node, self.parse_and())
        return node

    def parse_and(self) -> tuple:
        node = self.parse_not()
        while self.peek().kind == "and":
            self.next()
            node = ("and", node, self.parse_not())
        return node

    def parse_not(self) -> tuple:
        if self.peek().kind == "not":
            tok = self.next()
            return ("not", self.parse_not(), tok.pos)
        return self.parse_comparison()

    def parse_comparison(self) -> tuple:
        left = self.parse_addition()
        tok = self.peek()
        if tok.kind == "op" and tok.value in ("==", "!=", ">", ">=", "<", "<="):
            self.next()
            return ("cmp", tok.value, left, self.parse_addition())
        if tok.kind == "in":
            self.next()
            return ("cmp", "in", left, self.parse_addition())
        return left

    def parse_addition(self) -> tuple:
        node = self.parse_multiplication()
        while self.peek().kind == "op" and self.peek().value in ("+", "-"):
            op = self.next().value
            node = ("arith", op, node, self.parse_multiplication())
        return node

    def parse_multiplication(self) -> tuple:
        node = self.parse_unary()
        while self.peek().kind == "op" and self.peek().value in ("*", "/", "%"):
            op = self.next().value
            node = ("arith", op, node, self.parse_unary())
        return node

    def parse_unary(self) -> tuple:
        tok = self.peek()
        if tok.kind == "op" and tok.value == "-":
            self.next()
            return ("neg", self.parse_unary())
        if tok.kind == "not":
            self.next()
            return ("not", self.parse_unary(), tok.pos)
        return self.parse_primary()

    def parse_primary(self) -> tuple:
        tok = self.next()
        if tok.kind in ("number", "string", "bool", "null"):
            return ("lit", tok.value)
        if tok.kind == "op" and tok.value == "(":
            node = self.parse_or()
            self.expect("op", ")")
            return node
        if tok.kind == "op" and tok.value == "[":
            items: list[tuple] = []
            if not (self.peek().kind == "op" and self.peek().value == "]"):
                items.append(self.parse_or())
                while self.peek().kind == "op" and self.peek().value == ",":
                    self.next()
                    items.append(self.parse_or())
            self.expect("op", "]")
            return ("list", items)
        if tok.kind == "name":
            path = [tok.value]
            while self.peek().kind == "op" and self.peek().value == ".":
                self.next()
                part = self.expect("name")
                path.append(part.value)
            if self.peek().kind == "op" and self.peek().value == "(":
                # Only a bare whitelisted name can be called, never a path.
                if len(path) != 1:
                    raise ExpressionError("method calls are not allowed")
                self.next()
                args: list[tuple] = []
                if not (self.peek().kind == "op" and self.peek().value == ")"):
                    args.append(self.parse_or())
                    while self.peek().kind == "op" and self.peek().value == ",":
                        self.next()
                        args.append(self.parse_or())
                self.expect("op", ")")
                if path[0] not in _ALLOWED_FUNCTIONS:
                    raise ExpressionError(f"function {path[0]!r} is not allowed (at {tok.pos})")
                return ("call", path[0], args, tok.pos)
            return ("var", tuple(path), tok.pos)
        raise ExpressionError(f"unexpected token at position {tok.pos}")


# --------------------------------------------------------------------------- #
# Evaluator
# --------------------------------------------------------------------------- #

_ALLOWED_FUNCTIONS = {"len", "lower", "upper"}


def _resolve(path: tuple[str, ...], context: dict[str, Any]) -> Any:
    value: Any = context
    for part in path:
        if isinstance(value, dict):
            if part in value:
                value = value[part]
            else:
                return None
        else:
            # Dotted paths traverse payload dicts only. A non-dict value with a
            # further segment (e.g. x.__class__) is always an error — Python
            # attribute access is never exposed.
            raise ExpressionError(
                f"cannot look up {part!r}: value is not an object"
            )
    return value


def _eval(node: tuple, context: dict[str, Any]) -> Any:
    tag = node[0]
    if tag == "lit":
        return node[1]
    if tag == "list":
        return [_eval(item, context) for item in node[1]]
    if tag == "var":
        return _resolve(node[1], context)
    if tag == "call":
        name, args, pos = node[1], node[2], node[3]
        if name not in _ALLOWED_FUNCTIONS:
            raise ExpressionError(f"function {name!r} is not allowed (at {pos})")
        values = [_eval(a, context) for a in args]
        if name == "len":
            if len(values) != 1:
                raise ExpressionError("len() takes exactly one argument")
            try:
                return len(values[0])
            except TypeError:
                raise ExpressionError("len() requires a list or string")
        if name == "lower":
            if len(values) != 1 or not isinstance(values[0], str):
                raise ExpressionError("lower() requires one string argument")
            return values[0].lower()
        if name == "upper":
            if len(values) != 1 or not isinstance(values[0], str):
                raise ExpressionError("upper() requires one string argument")
            return values[0].upper()
    if tag == "neg":
        value = _eval(node[1], context)
        if value is None:
            return None
        if not isinstance(value, (int, float)):
            raise ExpressionError("unary minus requires a number")
        return -value
    if tag == "not":
        return not _eval(node[1], context)
    if tag == "and":
        return bool(_eval(node[1], context)) and bool(_eval(node[2], context))
    if tag == "or":
        return bool(_eval(node[1], context)) or bool(_eval(node[2], context))
    if tag == "arith":
        op, left, right = node[1], _eval(node[2], context), _eval(node[3], context)
        if left is None or right is None:
            return None
        if op in ("+", "-") and isinstance(left, str) and isinstance(right, str) and op == "+":
            return left + right
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            raise ExpressionError(f"operator {op!r} requires numbers")
        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "/":
            if right == 0:
                raise ExpressionError("division by zero")
            return left / right
        if op == "%":
            if right == 0:
                raise ExpressionError("modulo by zero")
            return left % right
    if tag == "cmp":
        op, left, right = node[1], _eval(node[2], context), _eval(node[3], context)
        if op == "in":
            if not isinstance(right, (list, tuple, str)):
                raise ExpressionError("'in' requires a list or string on the right")
            return left in right
        if left is None or right is None:
            # Missing data only matches explicit null-equality checks.
            return (op == "==" and left is None and right is None) or (
                op == "!=" and (left is None or right is None) and not (left is None and right is None)
            )
        try:
            if op == "==":
                return left == right
            if op == "!=":
                return left != right
            if op == ">":
                return left > right
            if op == ">=":
                return left >= right
            if op == "<":
                return left < right
            if op == "<=":
                return left <= right
        except TypeError:
            raise ExpressionError(f"cannot compare {type(left).__name__} and {type(right).__name__}")
    raise ExpressionError(f"invalid expression node {tag!r}")  # pragma: no cover


def compile_expression(src: str) -> tuple:
    """Parse once (at template publish time) and return the AST."""
    if not isinstance(src, str) or not src.strip():
        raise ExpressionError("expression must be a non-empty string")
    return _Parser(_tokenize(src)).parse()


def evaluate(src: str, context: dict[str, Any]) -> bool:
    """Evaluate an expression source against a payload and coerce to bool."""
    return bool(_eval(compile_expression(src), context or {}))
