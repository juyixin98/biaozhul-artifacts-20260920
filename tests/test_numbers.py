"""Number formats: INT / FLOAT / HEX disambiguation and the lex-error path."""

from __future__ import annotations

from conftest import create_ok

RULES = [
    {"name": "INT", "pattern": "[0-9]+", "priority": 0},
    {"name": "FLOAT", "pattern": "[0-9]+\\.[0-9]+", "priority": 1},
    {"name": "HEX", "pattern": "0x[0-9a-f]+", "priority": 2},
    {"name": "WS", "pattern": "[ \\t\\n]+", "priority": 3},
]


def test_number_formats_with_offsets(client):
    created = create_ok(client, "numbers", RULES)
    response = client.post(
        f"/api/rulesets/{created['ruleset_id']}/lex",
        json={"text": "42 3.14 0xff 007"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["error"] is None
    got = [(t["rule"], t["text"], t["start"], t["end"]) for t in data["tokens"]]
    assert got == [
        ("INT", "42", 0, 2),
        ("WS", " ", 2, 3),
        ("FLOAT", "3.14", 3, 7),   # longest match beats INT "3"
        ("WS", " ", 7, 8),
        ("HEX", "0xff", 8, 12),    # longest match beats INT "0"
        ("WS", " ", 12, 13),
        ("INT", "007", 13, 16),
    ]


def test_lex_error_reports_offset_and_char(client):
    created = create_ok(client, "numbers-err", RULES)
    response = client.post(
        f"/api/rulesets/{created['ruleset_id']}/lex",
        json={"text": "7."},
    )
    assert response.status_code == 200
    data = response.json()
    # The valid prefix is still returned; the error pinpoints the failure.
    assert [(t["rule"], t["text"], t["start"], t["end"]) for t in data["tokens"]] == [
        ("INT", "7", 0, 1)
    ]
    assert data["error"]["category"] == "INPUT_ERROR"
    assert data["error"]["offset"] == 1
    assert data["error"]["char"] == "."
