"""End-to-end HTTP tests with exact result assertions, not just liveness."""
from __future__ import annotations


def _create(client, name, content):
    resp = client.post("/documents", json={"name": name, "content": content})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] and body["data"]["version"] == 1
    assert body["request_id"]
    return body["data"]


def test_create_and_match_jump_api(client):
    doc = _create(client, "doc", "(a[b]c)")
    resp = client.get(f"/documents/{doc['id']}/match", params={"offset": 0})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["matched"] is True
    assert data["partner_offset"] == 6
    assert data["type"] == "paren"


def test_match_on_closer_jumps_back(client):
    doc = _create(client, "d", "([{}])")
    resp = client.get(f"/documents/{doc['id']}/match", params={"offset": 5})
    assert resp.json()["data"]["partner_offset"] == 0


def test_match_non_structural_offset_rejected_with_redaction(client):
    secret = "PASSW0RD"
    doc = _create(client, "s", f"({secret})")
    # offset 3 sits inside the secret payload; it must be rejected and the
    # response/logs must not echo it.
    resp = client.get(f"/documents/{doc['id']}/match", params={"offset": 3})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["reason"] == "NOT_STRUCTURAL"
    assert detail["request_id"]
    assert secret not in resp.text
    diag = client.get("/diagnostics").json()["data"]["records"]
    joined = str(diag)
    assert secret not in joined


def test_match_bracket_inside_string_is_rejected_not_matched(client):
    doc = _create(client, "masked", 'a"([)]"b')
    # offset 3 is the '[' inside the quoted span: structurally inert.
    resp = client.get(f"/documents/{doc['id']}/match", params={"offset": 3})
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "NOT_STRUCTURAL"
    # The diagnostic state distinguishes "bracket-shaped but masked" from
    # plain text.
    records = client.get("/diagnostics").json()["data"]["records"]
    rec = [r for r in records if r["operation"] == "match"
           and r["state"].get("offset") == 3][-1]
    assert rec["state"]["char_is_bracket"] is True


def test_crossing_types_report_mismatch_and_shortest_interval(client):
    doc = _create(client, "x", "([)]")
    resp = client.get(f"/documents/{doc['id']}/defects")
    data = resp.json()["data"]
    assert data["balanced"] is False
    # Shortest unbalanced interval is [1,3) (mismatch '[' vs ')'), shorter
    # than [0,4).
    assert data["shortest"]["category"] == "TYPE_MISMATCH"
    assert data["shortest"]["interval"] == [1, 3]
    assert data["shortest"]["type"] == "square"
    categories = sorted(d["category"] for d in data["defects"])
    assert categories == ["TYPE_MISMATCH", "TYPE_MISMATCH"]


def test_stale_edit_conflict_and_then_success(client):
    doc = _create(client, "v", "(abc)")
    ok = client.post(
        f"/documents/{doc['id']}/edits",
        json={"start": 0, "end": 0, "replacement": "[", "base_version": 1},
    )
    assert ok.status_code == 200
    assert ok.json()["data"]["version"] == 2
    stale = client.post(
        f"/documents/{doc['id']}/edits",
        json={"start": 1, "end": 1, "replacement": "x", "base_version": 1},
    )
    assert stale.status_code == 409
    body = stale.json()["detail"]
    assert body["reason"] == "VERSION_CONFLICT"
    assert body["error"]["state"]["current_version"] == 2
    # Retrying with the fresh version is accepted.
    retry = client.post(
        f"/documents/{doc['id']}/edits",
        json={"start": 1, "end": 1, "replacement": "x", "base_version": 2},
    )
    assert retry.status_code == 200
    assert retry.json()["data"]["version"] == 3


def test_edit_invalidation_report_is_local(client):
    # ~10 chunks worth of text; edit one character.
    content = "(" + "abcdefghij" * 12 + ")"
    doc = _create(client, "big", content)
    resp = client.post(
        f"/documents/{doc['id']}/edits",
        json={"start": 5, "end": 5, "replacement": "X", "base_version": 1},
    )
    data = resp.json()["data"]
    assert data["rescanned_chars"] < len(content) // 2
    assert data["blocks_removed"] <= 2


def test_verify_endpoint_reports_agreement(client):
    doc = _create(client, "ok", "(([{}]))" * 20)
    resp = client.get(f"/documents/{doc['id']}/verify")
    data = resp.json()["data"]
    assert data["agrees"] is True
    assert data["balanced"] is True


def test_stateless_analyze_escaped_string(client):
    resp = client.post(
        "/query/analyze", json={"content": '"a\\"([)]b" ()', "offset": 12}
    )
    data = resp.json()["data"]
    # The brackets in the string are content; only the final () match.
    assert data["balanced"] is True
    assert data["match_at"]["matched"] is True
    assert data["match_at"]["partner_offset"] == 11


def test_stateless_analyze_flags_unterminated_undetermined(client):
    resp = client.post(
        "/query/analyze", json={"content": 'abc " ( ['}
    )
    body = resp.json()
    assert body["status"] == "undetermined"
    assert "unterminated" in body["reason"]


def test_diagnostics_carry_request_id_and_state(client):
    doc = _create(client, "d", "()")
    rid = "test-rid-1234"
    resp = client.get(
        f"/documents/{doc['id']}/match",
        params={"offset": 0},
        headers={"X-Request-Id": rid},
    )
    assert resp.json()["request_id"] == rid
    records = client.get("/diagnostics").json()["data"]["records"]
    mine = [r for r in records if r["request_id"] == rid]
    assert mine and mine[-1]["outcome"] == "accepted"
    assert mine[-1]["state"]["partner"] == 1


def test_unknown_document_is_404_with_request_id(client):
    resp = client.get("/documents/4242/match", params={"offset": 0})
    assert resp.status_code == 404
    assert resp.json()["detail"]["request_id"]


def test_health(client):
    assert client.get("/health").json() == {"ok": True, "chunk_size": 16}
