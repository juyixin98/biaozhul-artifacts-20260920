"""Run logs keep run ids, intermediate state and decision rationales.

Failures of different categories leave distinguishable log entries, and a run
can be replayed via GET /api/runs/{run_id}.
"""

from __future__ import annotations

from conftest import create_ok, post_ruleset


def _replay(client, run_id):
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200
    return response.json()["entries"]


def test_successful_compile_run_is_replayable(client):
    rules = [
        {"name": "KW", "pattern": "if", "priority": 0},
        {"name": "ID", "pattern": "[a-z]+", "priority": 1},
    ]
    response = post_ruleset(client, "logged", rules)
    assert response.status_code == 201
    run_id = response.json()["run_id"]

    entries = _replay(client, run_id)
    stages = [entry["stage"] for entry in entries]
    assert stages == [
        "validate",
        "parse",
        "parse",
        "nfa",
        "dfa",
        "diagnostics",
        "stored",
    ]
    by_stage = {entry["stage"]: entry["detail"] for entry in entries}
    # Key intermediate state is retained, not just "it ran".
    assert by_stage["validate"]["rules"] == 2
    assert by_stage["validate"]["newline_mode"] == "LF"
    assert by_stage["nfa"]["states"] >= 2
    assert by_stage["dfa"]["states"] >= 2
    diag = by_stage["diagnostics"]
    assert diag["overlaps"] == [
        {"rule_a": "KW", "rule_b": "ID", "witness": "if"}
    ]
    assert diag["win_witness"]["KW"] == "if"
    assert diag["win_witness"]["ID"] != "if"  # rationale: ID shortest win != "if"


def test_lex_run_logs_tokens_and_offset(client):
    created = create_ok(
        client, "logged-lex", [{"name": "A", "pattern": "a+"},
                               {"name": "WS", "pattern": " +"}]
    )
    response = client.post(
        f"/api/rulesets/{created['ruleset_id']}/lex", json={"text": "aa a"}
    )
    run_id = response.json()["run_id"]
    entries = _replay(client, run_id)
    assert [e["stage"] for e in entries] == ["request", "lex"]
    assert entries[0]["detail"]["text_length"] == 4
    assert entries[1]["detail"]["token_count"] == 3
    assert entries[1]["detail"]["error"] is None


def test_error_categories_are_distinguishable_in_logs(client):
    cases = [
        ([{"name": "R", "pattern": "a*"}], "INPUT_ERROR"),
        ([{"name": "R", "pattern": "a{9999}"}], "RESOURCE_EXHAUSTED"),
    ]
    for idx, (rules, expected_category) in enumerate(cases):
        response = post_ruleset(client, f"cat-{idx}", rules)
        assert response.status_code in (400, 413)
        run_id = response.json()["run_id"]
        entries = _replay(client, run_id)
        error_entries = [e for e in entries if e["stage"] == "error"]
        assert len(error_entries) == 1
        assert error_entries[0]["detail"]["category"] == expected_category


def test_unknown_run_is_input_error(client):
    response = client.get("/api/runs/run_doesnotexist")
    assert response.status_code == 404
    assert response.json()["error"]["category"] == "INPUT_ERROR"
