"""Selector engine unit tests: label logic, never string matching."""

import pytest

from app.models import LabelSelector
from app.selectors import selector_matches


def parse(raw: dict) -> LabelSelector:
    return LabelSelector.model_validate(raw)


def test_empty_selector_matches_everything_including_empty_labels():
    s = parse({})
    assert selector_matches(s, {})
    assert selector_matches(s, {"a": "b"})


def test_match_labels_is_conjunctive_exact_equality():
    s = parse({"matchLabels": {"app": "api", "tier": "backend"}})
    assert selector_matches(s, {"app": "api", "tier": "backend", "x": "y"})
    assert not selector_matches(s, {"app": "api"})
    assert not selector_matches(s, {"app": "api2", "tier": "backend"})
    # Prefix-of-value must not match: proves no substring matching.
    assert not selector_matches(s, {"app": "ap", "tier": "backend"})


def test_in_and_notin_set_semantics():
    s = parse({
        "matchExpressions": [
            {"key": "env", "operator": "In", "values": ["dev", "staging"]},
            {"key": "app", "operator": "NotIn", "values": ["legacy"]},
        ]
    })
    assert selector_matches(s, {"env": "dev", "app": "web"})
    assert selector_matches(s, {"env": "staging"})  # missing key is OK for NotIn
    assert not selector_matches(s, {"env": "prod", "app": "web"})
    assert not selector_matches(s, {"env": "dev", "app": "legacy"})


def test_exists_and_doesnotexist():
    s = parse({
        "matchExpressions": [
            {"key": "canary", "operator": "Exists"},
            {"key": "debug", "operator": "DoesNotExist"},
        ]
    })
    assert selector_matches(s, {"canary": ""})  # Exists: any value incl. empty
    assert selector_matches(s, {"canary": "true"})
    assert not selector_matches(s, {"debug": "x"})
    assert not selector_matches(s, {})


def test_selector_key_name_must_not_match_as_substring():
    # Labels with similarly named keys must not satisfy a requirement.
    s = parse({"matchExpressions": [
        {"key": "app", "operator": "In", "values": ["web"]}
    ]})
    assert not selector_matches(s, {"application": "web"})
    assert not selector_matches(s, {"xapp": "web"})


def test_requirement_value_shapes_validated():
    with pytest.raises(Exception):
        parse({"matchExpressions": [
            {"key": "k", "operator": "In", "values": []}
        ]})
    with pytest.raises(Exception):
        parse({"matchExpressions": [
            {"key": "k", "operator": "Exists", "values": ["v"]}
        ]})
    with pytest.raises(Exception):
        parse({"matchExpressions": [{"key": "k", "operator": "Bogus"}]})
