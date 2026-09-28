"""Parsers for class expressions and axioms.

Two input surfaces are accepted, both producing the same immutable AST:

1. OWL-style functional syntax, e.g.
       SubClassOf(ObjectIntersectionOf(A B) C)
       EquivalentClasses(D ObjectIntersectionOf(A B))
       DisjointClasses(A B)
       ClassAssertion(A x)
2. A JSON constructor form (used by the HTTP API), e.g.
       {"type": "SubClassOf",
        "sub": {"type": "ObjectIntersectionOf", "operands": [...]},
        "sup": {"type": "Class", "name": "C"}}

Unsupported constructors raise ``LangError(UNSUPPORTED_CONSTRUCTOR)`` with a
``position`` pinpointing the rejected node -- they never degrade to labels.
"""

from __future__ import annotations

from . import ast
from .errors import UNSUPPORTED_CONSTRUCTORS, ErrorCode, LangError

__all__ = ["parse_axiom", "parse_axioms", "class_expr_from_json", "axiom_from_json"]

_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:.-")

# Axiom keywords that take class-expression arguments.
_AXIOM_TYPES = frozenset(
    {"SubClassOf", "EquivalentClasses", "DisjointClasses", "ClassAssertion"}
)
_CLASS_EXPR_TYPES = frozenset({"Class", "ObjectIntersectionOf"})


# --------------------------------------------------------------------------- #
# Functional-syntax tokenizer / parser
# --------------------------------------------------------------------------- #
def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "()":
            tokens.append(ch)
            i += 1
            continue
        if ch == "#":  # comment to end of line
            while i < n and text[i] != "\n":
                i += 1
            continue
        start = i
        while i < n and text[i] in _NAME_CHARS:
            i += 1
        if i == start:
            raise LangError(
                ErrorCode.MALFORMED_EXPRESSION,
                f"unexpected character {ch!r}",
                position=f"offset {start}",
            )
        tokens.append(text[start:i])
    return tokens


class _Parser:
    def __init__(self, tokens: list[str]) -> None:
        self._toks = tokens
        self._pos = 0

    def _peek(self) -> str | None:
        return self._toks[self._pos] if self._pos < len(self._toks) else None

    def _next(self) -> str:
        tok = self._peek()
        if tok is None:
            raise LangError(ErrorCode.MALFORMED_EXPRESSION, "unexpected end of expression")
        self._pos += 1
        return tok

    def _expect(self, expected: str) -> None:
        tok = self._next()
        if tok != expected:
            raise LangError(
                ErrorCode.MALFORMED_EXPRESSION,
                f"expected {expected!r} but found {tok!r}",
            )

    def parse_axiom(self) -> ast.Axiom:
        head = self._next()
        if head in UNSUPPORTED_CONSTRUCTORS:
            raise LangError(
                ErrorCode.UNSUPPORTED_CONSTRUCTOR,
                f"constructor/axiom {head!r} is not supported in the restricted fragment",
                position="axiom head",
            )
        if head not in _AXIOM_TYPES:
            # Unknown head that is *not* on the explicit reject list: still a
            # malformed input for this service; we never invent a label for it.
            raise LangError(
                ErrorCode.MALFORMED_EXPRESSION,
                f"unknown axiom type {head!r}; supported: {sorted(_AXIOM_TYPES)}",
            )
        self._expect("(")
        if head == "SubClassOf":
            sub = self._parse_class_expr()
            sup = self._parse_class_expr()
            axiom: ast.Axiom = ast.SubClassOf(sub=sub, sup=sup)
        elif head == "ClassAssertion":
            cls = self._parse_class_expr()
            individual = self._next()
            axiom = ast.ClassAssertion(individual=individual, cls=cls)
        else:
            operands = self._parse_class_list(head)
            if head == "EquivalentClasses":
                axiom = ast.EquivalentClasses(operands=operands)
            else:
                axiom = ast.DisjointClasses(operands=operands)
        self._expect(")")
        return axiom

    def _parse_class_list(self, head: str) -> tuple[ast.ClassExpr, ...]:
        operands: list[ast.ClassExpr] = []
        while self._peek() != ")":
            if self._peek() is None:
                raise LangError(
                    ErrorCode.MALFORMED_EXPRESSION,
                    f"unterminated {head}: missing closing ')'",
                )
            operands.append(self._parse_class_expr())
        if len(operands) < 2:
            raise LangError(
                ErrorCode.MALFORMED_EXPRESSION,
                f"{head} requires at least 2 class operands, got {len(operands)}",
            )
        return tuple(operands)

    def _parse_class_expr(self) -> ast.ClassExpr:
        head = self._next()
        if head == "(":
            expr = self._parse_class_expr()
            self._expect(")")
            return expr
        if head in UNSUPPORTED_CONSTRUCTORS:
            raise LangError(
                ErrorCode.UNSUPPORTED_CONSTRUCTOR,
                f"class constructor {head!r} is not supported in the restricted fragment",
                position="class expression",
            )
        if head == "ObjectIntersectionOf":
            self._expect("(")
            operands: list[ast.ClassExpr] = []
            while self._peek() != ")":
                if self._peek() is None:
                    raise LangError(
                        ErrorCode.MALFORMED_EXPRESSION,
                        "unterminated ObjectIntersectionOf",
                    )
                operands.append(self._parse_class_expr())
            self._expect(")")
            return _make_intersection(operands)
        if head is None:
            raise LangError(ErrorCode.MALFORMED_EXPRESSION, "expected class expression")
        # Any other bare token is a class name, including ones containing ':'
        # (CURIEs such as ex:A) -- but it must look like a name, not a keyword.
        if head in _AXIOM_TYPES:
            raise LangError(
                ErrorCode.MALFORMED_EXPRESSION,
                f"axiom keyword {head!r} used where a class expression was expected",
            )
        return ast.ClassName(name=head)


def _make_intersection(operands: list[ast.ClassExpr]) -> ast.ClassExpr:
    if not operands:
        raise LangError(
            ErrorCode.MALFORMED_EXPRESSION,
            "ObjectIntersectionOf requires at least 2 operands, got 0",
        )
    # Flatten nested intersections; de-duplicate while preserving first order.
    flat: list[ast.ClassExpr] = []
    seen: set[ast.ClassExpr] = set()

    def add(expr: ast.ClassExpr) -> None:
        if isinstance(expr, ast.ObjectIntersection):
            for op in expr.operands:
                add(op)
        elif expr not in seen:
            seen.add(expr)
            flat.append(expr)

    for op in operands:
        add(op)
    if len(flat) == 1:
        return flat[0]
    return ast.ObjectIntersection(operands=tuple(flat))


def parse_axiom(text: str) -> ast.Axiom:
    parser = _Parser(_tokenize(text))
    axiom = parser.parse_axiom()
    if parser._peek() is not None:
        raise LangError(
            ErrorCode.MALFORMED_EXPRESSION,
            f"unexpected trailing token {parser._peek()!r}",
        )
    return axiom


def parse_axioms(text: str) -> tuple[ast.Axiom, ...]:
    """Parse several whitespace-separated functional-syntax axioms.

    Top-level '(' ... ')' groups delimit axioms; a bare keyword also starts one.
    """
    tokens = _tokenize(text)
    axioms: list[ast.Axiom] = []
    parser = _Parser(tokens)
    while parser._peek() is not None:
        axioms.append(parser.parse_axiom())
    return tuple(axioms)


# --------------------------------------------------------------------------- #
# JSON constructor form
# --------------------------------------------------------------------------- #
def class_expr_from_json(node: dict, position: str = "class expression") -> ast.ClassExpr:
    if not isinstance(node, dict):
        raise LangError(ErrorCode.MALFORMED_EXPRESSION, "class expression must be an object", position=position)
    kind = node.get("type")
    if kind in UNSUPPORTED_CONSTRUCTORS:
        raise LangError(
            ErrorCode.UNSUPPORTED_CONSTRUCTOR,
            f"constructor {kind!r} is not supported in the restricted fragment",
            position=position,
        )
    if kind == "Class":
        name = node.get("name")
        if not isinstance(name, str) or not name:
            raise LangError(ErrorCode.MALFORMED_EXPRESSION, "Class requires non-empty 'name'", position=position)
        return ast.ClassName(name=name)
    if kind == "ObjectIntersectionOf":
        raw = node.get("operands")
        if not isinstance(raw, list) or len(raw) < 2:
            raise LangError(
                ErrorCode.MALFORMED_EXPRESSION,
                "ObjectIntersectionOf requires a list of at least 2 operands",
                position=position,
            )
        operands = [class_expr_from_json(op, f"{position}.operands[{i}]") for i, op in enumerate(raw)]
        return _make_intersection(operands)
    raise LangError(
        ErrorCode.MALFORMED_EXPRESSION,
        f"unknown class-expression type {kind!r}; supported: {sorted(_CLASS_EXPR_TYPES)}",
        position=position,
    )


def axiom_from_json(node: dict, index: int | None = None) -> ast.Axiom:
    pos = "axiom" if index is None else f"axiom[{index}]"
    if not isinstance(node, dict):
        raise LangError(ErrorCode.MALFORMED_EXPRESSION, "axiom must be an object", position=pos)
    kind = node.get("type")
    if kind in UNSUPPORTED_CONSTRUCTORS:
        raise LangError(
            ErrorCode.UNSUPPORTED_CONSTRUCTOR,
            f"axiom/constructor {kind!r} is not supported in the restricted fragment",
            position=pos,
        )
    if kind == "SubClassOf":
        return ast.SubClassOf(
            sub=class_expr_from_json(node["sub"], f"{pos}.sub"),
            sup=class_expr_from_json(node["sup"], f"{pos}.sup"),
        )
    if kind in ("EquivalentClasses", "DisjointClasses"):
        raw = node.get("operands")
        if not isinstance(raw, list) or len(raw) < 2:
            raise LangError(
                ErrorCode.MALFORMED_EXPRESSION,
                f"{kind} requires a list of at least 2 operands",
                position=pos,
            )
        operands = tuple(
            class_expr_from_json(op, f"{pos}.operands[{i}]") for i, op in enumerate(raw)
        )
        cls_ = ast.EquivalentClasses if kind == "EquivalentClasses" else ast.DisjointClasses
        return cls_(operands=operands)
    if kind == "ClassAssertion":
        individual = node.get("individual")
        if not isinstance(individual, str) or not individual:
            raise LangError(
                ErrorCode.MALFORMED_EXPRESSION,
                "ClassAssertion requires non-empty 'individual'",
                position=pos,
            )
        return ast.ClassAssertion(
            individual=individual,
            cls=class_expr_from_json(node["class"], f"{pos}.class"),
        )
    raise LangError(
        ErrorCode.MALFORMED_EXPRESSION,
        f"unknown axiom type {kind!r}; supported: {sorted(_AXIOM_TYPES)}",
        position=pos,
    )
