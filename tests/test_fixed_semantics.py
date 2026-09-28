"""Fixed Unicode ranges and fixed LF newline mode.

These semantics are corpus-level constants, not configuration:

- ``.`` does not match U+000A (LF); CR is an ordinary character.
- negated classes ARE complemented over the whole alphabet, so they match LF.
- ``\\d``/``\\w``/``\\s`` are ASCII-only; non-ASCII digits are not digits.
"""

from __future__ import annotations

from conftest import create_ok


def _lex(client, name, rules, text):
    created = create_ok(client, name, rules)
    response = client.post(
        f"/api/rulesets/{created['ruleset_id']}/lex", json={"text": text}
    )
    assert response.status_code == 200
    return response.json()


def test_dot_excludes_lf_but_cr_is_ordinary(client):
    rules = [{"name": "ANY", "pattern": "."}, {"name": "NL", "pattern": "\n"}]
    data = _lex(client, "dot", rules, "a\nb")
    assert [(t["rule"], t["text"]) for t in data["tokens"]] == [
        ("ANY", "a"),
        ("NL", "\n"),
        ("ANY", "b"),
    ]
    # CR is not a newline in LF mode.
    data = _lex(client, "dot-cr", rules, "a\r")
    assert [(t["rule"], t["text"]) for t in data["tokens"]] == [
        ("ANY", "a"),
        ("ANY", "\r"),
    ]


def test_negated_class_matches_newline(client):
    rules = [{"name": "NOT_X", "pattern": "[^x]"}]
    data = _lex(client, "neg", rules, "\n\r")
    assert [t["text"] for t in data["tokens"]] == ["\n", "\r"]


def test_ascii_only_char_classes(client):
    rules = [
        {"name": "D", "pattern": "\\d"},
        {"name": "W", "pattern": "\\w"},
        {"name": "S", "pattern": "\\s"},
        {"name": "OTHER", "pattern": "."},
    ]
    # U+0661 ARABIC-INDIC DIGIT ONE is a Unicode digit but not an ASCII one.
    data = _lex(client, "ascii-classes", rules, "١")
    assert data["tokens"][0]["rule"] == "OTHER"
    # A regular ASCII digit matches D.
    data = _lex(client, "ascii-digit", rules, "7")
    assert data["tokens"][0]["rule"] == "D"
    # \s covers tab/LF/CR/space (0x09..0x0D plus 0x20).
    for ws in "\t\n\r\f\v ":
        data = _lex(client, f"space-{ord(ws)}", rules, ws)
        assert data["tokens"][0]["rule"] == "S"


def test_unicode_literal_and_offsets(client):
    rules = [{"name": "SNOW", "pattern": "☃+"}, {"name": "X", "pattern": "."}]
    data = _lex(client, "snowman", rules, "☃☃a")
    tokens = data["tokens"]
    assert tokens[0]["rule"] == "SNOW"
    assert tokens[0]["text"] == "☃☃"
    assert (tokens[0]["start"], tokens[0]["end"]) == (0, 2)
    assert tokens[1]["rule"] == "X"
    assert (tokens[1]["start"], tokens[1]["end"]) == (2, 3)
