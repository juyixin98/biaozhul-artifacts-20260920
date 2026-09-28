"""端到端 API：规范登记、构建、诊断、词法执行、错误类别、日志重放。"""

KEYWORD_SPEC = {
    "name": "keywords",
    "version": "1",
    "rules": [
        {"name": "IF", "pattern": "if", "priority": 10},
        {"name": "ELSE", "pattern": "else", "priority": 10},
        {"name": "IDENT", "pattern": "[A-Za-z_][A-Za-z0-9_]*", "priority": 0},
        {"name": "WS", "pattern": "[ \\t\\n]+", "priority": 0, "skip": True},
    ],
}


def _create_spec(client, spec=None):
    return client.post("/specs", json=spec or KEYWORD_SPEC)


def _build_lexer(client, spec_id):
    return client.post("/lexers", json={"spec_id": spec_id})


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_full_flow_build_and_tokenize(client):
    spec_id = _create_spec(client).json()["spec_id"]
    built = _build_lexer(client, spec_id)
    assert built.status_code == 201
    body = built.json()
    assert body["status"] == "ready"
    assert body["stats"]["rules"] == 4
    # IF/ELSE 与 IDENT 重叠，见证为关键字自身
    overlaps = {(o["rule_a"], o["rule_b"]): o["witness"] for o in body["diagnostics"]["overlaps"]}
    assert overlaps[("IF", "IDENT")] == "if"
    assert overlaps[("ELSE", "IDENT")] == "else"
    assert all(not u["unreachable"] for u in body["diagnostics"]["unreachable"])

    lexer_id = body["lexer_id"]
    resp = client.post(f"/lexers/{lexer_id}/tokenize", json={"text": "if iffy else"})
    assert resp.status_code == 200
    tokens = resp.json()["tokens"]
    assert tokens == [
        {"type": "IF", "start": 0, "end": 2, "text": "if"},
        {"type": "IDENT", "start": 3, "end": 7, "text": "iffy"},
        {"type": "ELSE", "start": 8, "end": 12, "text": "else"},
    ]


def test_diagnostics_persisted_and_queryable(client):
    spec_id = _create_spec(client).json()["spec_id"]
    lexer_id = _build_lexer(client, spec_id).json()["lexer_id"]
    got = client.get(f"/lexers/{lexer_id}")
    assert got.status_code == 200
    diag = got.json()["diagnostics"]
    assert any(o["witness"] == "if" for o in diag["overlaps"])


def test_duplicate_spec_is_state_conflict(client):
    assert _create_spec(client).status_code == 201
    dup = _create_spec(client)
    assert dup.status_code == 409
    err = dup.json()["error"]
    assert err["category"] == "state_conflict"
    assert err["code"] == "SPEC_EXISTS"


def test_bad_pattern_is_input_error(client):
    spec = {"name": "bad", "rules": [{"name": "A", "pattern": "a("}]}
    resp = _create_spec(client, spec)
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["category"] == "input_error"
    assert err["code"] == "REGEX_SYNTAX"


def test_empty_match_rule_is_input_error(client):
    spec = {"name": "empty", "rules": [{"name": "A", "pattern": "a*"}]}
    resp = _create_spec(client, spec)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "EMPTY_MATCH"


def test_oversized_repeat_is_resource_exhausted(client):
    spec = {"name": "huge", "rules": [{"name": "A", "pattern": "a{2000}"}]}
    resp = _create_spec(client, spec)
    assert resp.status_code == 413
    assert resp.json()["error"]["category"] == "resource_exhausted"


def test_schema_violation_is_input_error(client):
    spec = {"name": "BadName", "rules": [{"name": "a", "pattern": "x"}]}
    resp = _create_spec(client, spec)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "REQUEST_INVALID"


def test_lex_failure_reports_offset(client):
    spec = {"name": "nums", "rules": [{"name": "INT", "pattern": "[0-9]+"}]}
    spec_id = _create_spec(client, spec).json()["spec_id"]
    lexer_id = _build_lexer(client, spec_id).json()["lexer_id"]
    resp = client.post(f"/lexers/{lexer_id}/tokenize", json={"text": "12@"})
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["category"] == "input_error"
    assert err["code"] == "LEX_NO_MATCH"
    assert err["details"]["offset"] == 2


def test_missing_lexer_is_input_error(client):
    resp = client.post("/lexers/999/tokenize", json={"text": "x"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "LEXER_NOT_FOUND"


def test_run_logs_replayable(client):
    spec_id = _create_spec(client).json()["spec_id"]
    run_id = _build_lexer(client, spec_id).json()["run_id"]
    logs = client.get(f"/runs/{run_id}/logs").json()
    assert logs["run_id"] == run_id
    entries = logs["entries"]
    assert entries, "运行日志不能为空"
    assert [e["seq"] for e in entries] == list(range(len(entries)))
    events = {e["event"] for e in entries}
    assert {"spec_validated", "dfa_built", "overlap_checked", "lexer_built"} <= events
    dfa_built = next(e for e in entries if e["event"] == "dfa_built")
    assert dfa_built["state"]["dfa_states"] > 0
    assert all(e["rationale"] for e in entries), "每条日志须带判断理由"
