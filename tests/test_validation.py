"""Request validation and the four-category error taxonomy."""

from __future__ import annotations

from conftest import create_ok, post_ruleset


def test_empty_matching_rules_are_rejected(client):
    nullable_patterns = ["a*", "(ab)?", "x{0,3}", "(a|)", "a*b?"]
    for pattern in nullable_patterns:
        response = post_ruleset(
            client, f"nullable-{pattern}", [{"name": "BAD", "pattern": pattern}]
        )
        assert response.status_code == 400, pattern
        error = response.json()["error"]
        assert error["category"] == "INPUT_ERROR"
        assert "empty string" in error["message"]
        assert error["detail"]["rule"] == "BAD"


def test_non_nullable_optional_constructs_are_allowed(client):
    for pattern in ["a+", "a?b", "x{0,3}y", "(ab)*c"]:
        response = post_ruleset(
            client, f"nonnull-{pattern}", [{"name": "OK", "pattern": pattern}]
        )
        assert response.status_code == 201, (pattern, response.json())


def test_bad_regex_syntax_is_input_error(client):
    response = post_ruleset(client, "bad-syntax", [{"name": "R", "pattern": "("}])
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["category"] == "INPUT_ERROR"
    assert "unclosed group" in error["message"]


def test_invalid_char_class_range_is_input_error(client):
    response = post_ruleset(
        client, "bad-range", [{"name": "R", "pattern": "[z-a]"}]
    )
    assert response.status_code == 400
    assert response.json()["error"]["category"] == "INPUT_ERROR"


def test_duplicate_rule_name_in_body_is_input_error(client):
    response = post_ruleset(
        client,
        "dup-rule",
        [
            {"name": "R", "pattern": "a"},
            {"name": "R", "pattern": "b"},
        ],
    )
    assert response.status_code == 400
    assert response.json()["error"]["category"] == "INPUT_ERROR"


def test_duplicate_ruleset_name_is_state_conflict(client):
    rules = [{"name": "R", "pattern": "a"}]
    assert post_ruleset(client, "same-name", rules).status_code == 201
    second = post_ruleset(client, "same-name", rules)
    assert second.status_code == 409
    assert second.json()["error"]["category"] == "STATE_CONFLICT"


def test_unknown_ruleset_and_missing_text_are_input_errors(client):
    assert client.get("/api/rulesets/rs_missing").status_code == 404
    response = client.post("/api/rulesets/rs_missing/lex", json={"text": "a"})
    assert response.status_code == 404
    assert response.json()["error"]["category"] == "INPUT_ERROR"

    created = create_ok(client, "lex-no-text", [{"name": "R", "pattern": "a"}])
    bad = client.post(
        f"/api/rulesets/{created['ruleset_id']}/lex", json={"nope": 1}
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["category"] == "INPUT_ERROR"


def test_repeat_over_limit_is_resource_exhausted(client):
    response = post_ruleset(
        client, "huge-repeat", [{"name": "R", "pattern": "a{5000}"}]
    )
    assert response.status_code == 413
    error = response.json()["error"]
    assert error["category"] == "RESOURCE_EXHAUSTED"
    assert "5000" in error["message"] and "1000" in error["message"]


def test_oversized_input_is_resource_exhausted(small_input_client):
    created = create_ok(
        small_input_client, "big-input", [{"name": "R", "pattern": "a+"}]
    )
    response = small_input_client.post(
        f"/api/rulesets/{created['ruleset_id']}/lex", json={"text": "a" * 100}
    )
    assert response.status_code == 413
    assert response.json()["error"]["category"] == "RESOURCE_EXHAUSTED"


def test_too_many_rules_is_resource_exhausted(client):
    rules = [{"name": f"R{i}", "pattern": f"a{i}"} for i in range(200)]
    response = post_ruleset(client, "too-many", rules)
    assert response.status_code == 413
    assert response.json()["error"]["category"] == "RESOURCE_EXHAUSTED"
