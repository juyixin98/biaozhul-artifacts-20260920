"""Comments: a line comment swallows keywords up to (not including) newline."""

from __future__ import annotations

from conftest import create_ok

RULES = [
    {"name": "LINE_COMMENT", "pattern": "//[^\\n]*", "priority": 0},
    {"name": "KW", "pattern": "if|else", "priority": 1},
    {"name": "ID", "pattern": "[a-z]+", "priority": 2},
    {"name": "INT", "pattern": "[0-9]+", "priority": 3},
    {"name": "WS", "pattern": "[ \\t\\n]+", "priority": 4},
]

SOURCE = "if x // if else\n42"


def test_comment_swallows_keywords_until_newline(client):
    created = create_ok(client, "comments", RULES)
    response = client.post(
        f"/api/rulesets/{created['ruleset_id']}/lex", json={"text": SOURCE}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["error"] is None
    got = [(t["rule"], t["text"], t["start"], t["end"]) for t in data["tokens"]]
    assert got == [
        ("KW", "if", 0, 2),
        ("WS", " ", 2, 3),
        ("ID", "x", 3, 4),
        ("WS", " ", 4, 5),
        # The keywords inside the comment are NOT tokenised as KW.
        ("LINE_COMMENT", "// if else", 5, 15),
        ("WS", "\n", 15, 16),
        ("INT", "42", 16, 18),
    ]


def test_offsets_slice_back_to_source(client):
    created = create_ok(client, "comments-offsets", RULES)
    response = client.post(
        f"/api/rulesets/{created['ruleset_id']}/lex", json={"text": SOURCE}
    )
    tokens = response.json()["tokens"]
    # Original offsets: every token text is exactly the source slice, and the
    # tokens tile the input contiguously.
    cursor = 0
    for token in tokens:
        assert token["start"] == cursor
        assert SOURCE[token["start"] : token["end"]] == token["text"]
        cursor = token["end"]
    assert cursor == len(SOURCE)
