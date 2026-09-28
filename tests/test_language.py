"""Unit tests: unsupported constructs are explicitly refused, not labeled."""
from __future__ import annotations

import pytest

from app.language import LanguageRejection, parse_ontology, parse_expression


def _reject(payload):
    with pytest.raises(LanguageRejection) as exc:
        parse_ontology(payload)
    return exc.value


@pytest.mark.unit
def test_union_constructor_is_refused_with_code():
    err = _reject({
        "axioms": [
            {"sub": "A", "super": {"union": ["B", "C"]}},
        ]
    })
    assert err.code == "UNSUPPORTED_CONSTRUCTOR"
    assert err.path == "$.axioms[0].super.union"


@pytest.mark.unit
def test_complement_is_refused():
    err = _reject({
        "axioms": [{"sub": "A", "super": {"complement": "B"}}]
    })
    assert err.code == "UNSUPPORTED_CONSTRUCTOR"


@pytest.mark.unit
def test_cardinality_restriction_is_refused():
    err = _reject({
        "axioms": [{
            "sub": "A",
            "super": {"cardinality": 2, "onProperty": "hasChild"},
        }]
    })
    assert err.code == "UNSUPPORTED_CONSTRUCTOR"


@pytest.mark.unit
def test_unknown_axiom_form_is_refused():
    err = _reject({
        "axioms": [{"propertyDomain": "p", "domain": "A"}]
    })
    assert err.code == "UNSUPPORTED_AXIOM"


@pytest.mark.unit
def test_reserved_owl_vocabulary_not_treated_as_label():
    err = _reject({
        "axioms": [{"sub": "A", "super": "owl:unionOf"}]
    })
    assert err.code == "RESERVED_VOCABULARY"


@pytest.mark.unit
def test_empty_intersection_rejected():
    err = _reject({
        "axioms": [{"sub": "A", "super": {"intersection": []}}]
    })
    assert err.code == "BAD_INTERSECTION_ARITY"


@pytest.mark.unit
def test_unary_disjoint_rejected():
    err = _reject({"axioms": [{"disjoint": ["A"]}]})
    assert err.code == "BAD_DISJOINT_ARITY"


@pytest.mark.unit
def test_missing_axiom_kind_rejected():
    err = _reject({"axioms": [{"source": "x"}]})
    assert err.code == "MISSING_AXIOM_KIND"


@pytest.mark.unit
def test_missing_super_field_rejected():
    err = _reject({"axioms": [{"sub": "A"}]})
    assert err.code == "MISSING_AXIOM_FIELD"


@pytest.mark.unit
def test_non_string_iri_rejected():
    with pytest.raises(LanguageRejection) as exc:
        parse_expression(123)
    assert exc.value.code == "UNSUPPORTED_EXPRESSION"


@pytest.mark.unit
def test_supported_fragment_is_accepted():
    onto = parse_ontology({"axioms": [
        {"sub": "A", "super": {"intersection": ["B", "C"]}},
        {"equivalent": ["D", "E"]},
        {"disjoint": ["F", "G", "H"]},
        {"instance": "i1", "class": "D"},
    ]})
    assert len(onto.axioms) == 4


@pytest.mark.unit
def test_iri_object_form_accepted():
    onto = parse_ontology({"axioms": [
        {"sub": {"iri": "A"}, "super": {"iri": "B"}},
    ]})
    assert onto.axioms[0].sub.iri == "A"
