"""Typed error taxonomy shared by parser, compiler, kernel and service.

Every failure the system can produce maps to one stable ``code`` so that tests
can assert the *failure category*, not just a non-200 HTTP status.
"""

from __future__ import annotations

__all__ = [
    "ErrorCode",
    "LangError",
    "UNSUPPORTED_CONSTRUCTORS",
]


class ErrorCode:
    # Language / parsing (4xx)
    UNSUPPORTED_CONSTRUCTOR = "UNSUPPORTED_CONSTRUCTOR"
    MALFORMED_EXPRESSION = "MALFORMED_EXPRESSION"
    UNKNOWN_CLASS = "UNKNOWN_CLASS"
    UNKNOWN_INDIVIDUAL = "UNKNOWN_INDIVIDUAL"
    BAD_REFERENCE = "BAD_REFERENCE"
    # Reasoning state (200 with findings, also raised with detail)
    UNSATISFIABLE_CLASS = "UNSATISFIABLE_CLASS"
    ONTOLOGY_INCONSISTENT = "ONTOLOGY_INCONSISTENT"
    MUTEX_INSTANCE_CONFLICT = "MUTEX_INSTANCE_CONFLICT"
    # Request / storage
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"


# Constructors that exist in full OWL but are deliberately rejected here.
# Listing them explicitly is what lets us say "rejected, not treated as a label".
UNSUPPORTED_CONSTRUCTORS = frozenset(
    {
        "ObjectUnionOf",
        "ObjectComplementOf",
        "ObjectOneOf",
        "ObjectSomeValuesFrom",
        "ObjectAllValuesFrom",
        "ObjectHasValue",
        "ObjectHasSelf",
        "ObjectMinCardinality",
        "ObjectMaxCardinality",
        "ObjectExactCardinality",
        "DataSomeValuesFrom",
        "DataAllValuesFrom",
        "DataHasValue",
        "DataMinCardinality",
        "DataMaxCardinality",
        "DataExactCardinality",
        "DataIntersectionOf",
        "DataUnionOf",
        "DataComplementOf",
        "DataOneOf",
        "DatatypeRestriction",
        "ObjectInverseOf",
        "ObjectPropertyChain",
        "SubObjectPropertyOf",
        "TransitiveObjectProperty",
        "FunctionalObjectProperty",
        "InverseFunctionalObjectProperty",
        "SymmetricObjectProperty",
        "AsymmetricObjectProperty",
        "ReflexiveObjectProperty",
        "IrreflexiveObjectProperty",
        "ObjectPropertyDomain",
        "ObjectPropertyRange",
        "SameIndividual",
        "DifferentIndividuals",
        "AnnotationAssertion",
        "Declaration",
        "Ontology",
        "Import",
        "Prefix",
    }
)


class LangError(Exception):
    """Error carrying a stable category code plus human/explanatory detail."""

    def __init__(self, code: str, message: str, *, position: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        # ``position`` pinpoints the rejected location, e.g. "axiom[3].rhs".
        self.position = position

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.position:
            d["position"] = self.position
        return d
