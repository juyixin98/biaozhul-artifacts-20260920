"""Rule language: the surface syntax accepted by the service.

Only a *restricted* fragment of OWL class expressions is supported:

* class/instance IRIs                       (bare strings or ``{"iri": ...}``)
* intersection of class expressions         ``{"intersection": [..]}``)
* subclass axiom                            ``{"sub": A, "super": B}``
* equivalence axiom (2+ operands)           ``{"equivalent": [..]}``
* disjointness axiom (2+ operands)          ``{"disjoint": [..]}``
* class assertion for an instance           ``{"instance": i, "class": C}``

Anything else (union, complement, cardinality, restriction, existential,
enumeration, SWRL rules, unknown keys, ...) is rejected explicitly by
:class:`LanguageRejection` -- it is never silently treated as a plain label.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Supported vocabulary
# ---------------------------------------------------------------------------

SUPPORTED_AXIOMS = frozenset({"sub", "equivalent", "disjoint", "instance"})
SUPPORTED_CONSTRUCTORS = frozenset({"intersection"})
# Anything outside these sets is an explicit refusal, not a label.

TOP = "owl:Thing"
BOTTOM = "owl:Nothing"
RESERVED = frozenset({TOP, BOTTOM})


class LanguageRejection(ValueError):
    """Raised when input uses a construct the restricted language forbids.

    The ``code`` is machine-readable (used in API error bodies) and ``path``
    pinpoints the offending JSON location.
    """

    def __init__(self, message: str, *, code: str, path: str = "$"):
        super().__init__(message)
        self.code = code
        self.path = path


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassRef:
    """A named class IRI. ``synthetic`` marks BOTTOM created by the kernel."""

    iri: str
    synthetic: bool = False


@dataclass(frozen=True)
class Intersection:
    operands: tuple["Expr", ...]


Expr = ClassRef | Intersection


@dataclass(frozen=True)
class SubClass:
    sub: Expr
    super: Expr
    source: str


@dataclass(frozen=True)
class Equivalent:
    operands: tuple[Expr, ...]
    source: str


@dataclass(frozen=True)
class Disjoint:
    operands: tuple[Expr, ...]
    source: str


@dataclass(frozen=True)
class ClassAssertion:
    instance: str
    cls: Expr
    source: str


Axiom = SubClass | Equivalent | Disjoint | ClassAssertion


@dataclass(frozen=True)
class Ontology:
    axioms: tuple[Axiom, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_iri(node: Any, path: str) -> str:
    if isinstance(node, str) and node.strip():
        iri = node.strip()
    elif isinstance(node, Mapping) and set(node) <= {"iri"} and \
            isinstance(node.get("iri"), str) and node["iri"].strip():
        iri = node["iri"].strip()
    else:
        raise LanguageRejection(
            f"expected a class/instance IRI (non-empty string), got {node!r}",
            code="UNSUPPORTED_EXPRESSION",
            path=path,
        )
    if iri.startswith(("http://www.w3.org/2002/07/owl#", "owl:")) and \
            iri not in RESERVED:
        # Other built-in OWL names (e.g. owl:unionOf smuggled in as an IRI)
        # must not be accepted as ordinary labels.
        raise LanguageRejection(
            f"reserved OWL vocabulary {iri!r} is not a user-definable label",
            code="RESERVED_VOCABULARY",
            path=path,
        )
    return iri


def parse_expression(node: Any, path: str = "$") -> Expr:
    """Parse one class expression, rejecting unsupported constructors."""
    if isinstance(node, str) or (
        isinstance(node, Mapping) and set(node) <= {"iri"}
    ):
        return ClassRef(_parse_iri(node, path))

    if not isinstance(node, Mapping):
        raise LanguageRejection(
            f"unsupported class expression {node!r}",
            code="UNSUPPORTED_EXPRESSION",
            path=path,
        )

    unknown = set(node) - SUPPORTED_CONSTRUCTORS
    if unknown:
        # e.g. {"union": [...]}, {"complement": ...}, {"cardinality": 2}
        raise LanguageRejection(
            f"unsupported OWL constructor(s): {sorted(unknown)}. This service "
            f"supports only {sorted(SUPPORTED_CONSTRUCTORS)}; it does not "
            "implement full OWL.",
            code="UNSUPPORTED_CONSTRUCTOR",
            path=f"{path}.{sorted(unknown)[0]}",
        )

    items = node["intersection"]
    if not isinstance(items, list) or len(items) < 2:
        raise LanguageRejection(
            "'intersection' needs a list of at least 2 operands",
            code="BAD_INTERSECTION_ARITY",
            path=f"{path}.intersection",
        )
    operands = tuple(
        parse_expression(item, f"{path}.intersection[{i}]")
        for i, item in enumerate(items)
    )
    return Intersection(operands)


def parse_axiom(node: Any, index: int) -> Axiom:
    path = f"$.axioms[{index}]"
    if not isinstance(node, Mapping):
        raise LanguageRejection(
            f"axiom must be an object, got {node!r}",
            code="UNSUPPORTED_EXPRESSION",
            path=path,
        )
    keys = set(node)
    # "super" is the second field of a subclass axiom, "class" the type field
    # of an instance assertion; the discriminators live in SUPPORTED_AXIOMS.
    allowed_fields = SUPPORTED_AXIOMS | {"super", "class", "source"}
    unknown = keys - allowed_fields
    if unknown:
        raise LanguageRejection(
            f"unsupported axiom form(s): {sorted(unknown)}. Supported axioms: "
            f"{sorted(SUPPORTED_AXIOMS)}",
            code="UNSUPPORTED_AXIOM",
            path=f"{path}.{sorted(unknown)[0]}",
        )

    kind = next((k for k in ("sub", "equivalent", "disjoint", "instance")
                 if k in node), None)
    if kind is None:
        raise LanguageRejection(
            "axiom object must contain one of "
            f"{sorted(SUPPORTED_AXIOMS)}",
            code="MISSING_AXIOM_KIND",
            path=path,
        )

    source = node.get("source", f"axiom[{index}]")

    if kind == "sub":
        missing = {"sub", "super"} - keys
        if missing:
            raise LanguageRejection(
                f"subclass axiom needs both 'sub' and 'super', missing {sorted(missing)}",
                code="MISSING_AXIOM_FIELD",
                path=path,
            )
        return SubClass(
            parse_expression(node["sub"], f"{path}.sub"),
            parse_expression(node["super"], f"{path}.super"),
            source,
        )

    key = kind
    items = node[key]
    if kind == "instance":
        instance = node.get("instance")
        cls = node.get("class")
        if instance is None or cls is None:
            raise LanguageRejection(
                "instance assertion needs both 'instance' and 'class'",
                code="MISSING_AXIOM_FIELD",
                path=path,
            )
        return ClassAssertion(
            _parse_iri(instance, f"{path}.instance"),
            parse_expression(cls, f"{path}.class"),
            source,
        )

    if not isinstance(items, list) or len(items) < 2:
        raise LanguageRejection(
            f"{kind!r} axiom needs a list of at least 2 operands",
            code=f"BAD_{kind.upper()}_ARITY",
            path=f"{path}.{kind}",
        )
    operands = tuple(
        parse_expression(item, f"{path}.{kind}[{i}]")
        for i, item in enumerate(items)
    )
    if kind == "equivalent":
        return Equivalent(operands, source)
    return Disjoint(operands, source)


def parse_ontology(payload: Any) -> Ontology:
    """Parse the full request payload ``{"axioms": [...]}``."""
    if isinstance(payload, Ontology):
        return payload
    if not isinstance(payload, Mapping):
        raise LanguageRejection(
            f"ontology payload must be an object, got {payload!r}",
            code="UNSUPPORTED_EXPRESSION",
        )
    if "axioms" not in payload:
        raise LanguageRejection(
            "payload must contain an 'axioms' list",
            code="MISSING_AXIOMS",
        )
    raw = payload["axioms"]
    if not isinstance(raw, list):
        raise LanguageRejection(
            "'axioms' must be a list", code="MISSING_AXIOMS"
        )
    axioms = tuple(parse_axiom(a, i) for i, a in enumerate(raw))
    return Ontology(axioms=axioms)
