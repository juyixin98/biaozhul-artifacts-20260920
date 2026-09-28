"""Query validation boundary: validate raw request bodies against the corpus
schema and the configured resource limits before they reach the kernel."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..config import Settings
from ..corpus.spec import RuleSetSpec
from ..errors import input_error, resource_exhausted


def _json_safe(value: Any) -> Any:
    """Strip non-JSON-serialisable objects pydantic can embed (e.g. the
    original ValueError inside a model_validator error's ``ctx``)."""
    if isinstance(value, dict):
        return {key: _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def parse_ruleset_request(body: Any, settings: Settings) -> RuleSetSpec:
    if not isinstance(body, dict):
        raise input_error("ruleset request body must be a JSON object")
    try:
        spec = RuleSetSpec.model_validate(body)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        raise input_error(
            f"invalid ruleset spec: {first.get('msg', exc.title)}",
            detail={"errors": _json_safe(exc.errors())},
        ) from exc
    if len(spec.rules) > settings.max_rules:
        raise resource_exhausted(
            f"ruleset has {len(spec.rules)} rules, limit is {settings.max_rules}"
        )
    for rule in spec.rules:
        if len(rule.pattern) > settings.max_pattern_chars:
            raise resource_exhausted(
                f"pattern of rule {rule.name!r} has {len(rule.pattern)} "
                f"characters, limit is {settings.max_pattern_chars}"
            )
    return spec


def parse_lex_request(body: Any, settings: Settings) -> str:
    if not isinstance(body, dict):
        raise input_error("lex request body must be a JSON object")
    text = body.get("text")
    if not isinstance(text, str):
        raise input_error("lex request requires a string field 'text'")
    if len(text) > settings.max_input_chars:
        raise resource_exhausted(
            f"input text has {len(text)} characters, limit is "
            f"{settings.max_input_chars}"
        )
    return text
