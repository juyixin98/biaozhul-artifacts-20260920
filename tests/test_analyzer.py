"""Unit + integration tests for the taint analyzer and HTTP API."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.analyzer import AnalysisError, Analyzer
from app.crypto_sign import verify
from app.lexer import Lexer
from app.parser import ParseError, parse
from app.web import app

FIXTURES = Path(__file__).parent / "fixtures"
client = TestClient(app)


def analyze_code(code: str) -> dict:
    return Analyzer(parse(code)).analyze()


# --------------------------------------------------------------------------- #
# lexer / parser
# --------------------------------------------------------------------------- #


def test_lexer_basic_tokens():
    toks = Lexer('x = source(); # comment\n').tokenize()
    types = [t.type.name for t in toks]
    assert types[:5] == ["NAME", "ASSIGN", "NAME", "LPAREN", "RPAREN"]


def test_parser_rejects_redefined_function():
    code = "func f() { return 0; } func f() { return 1; }"
    with pytest.raises(ParseError, match="redefined"):
        parse(code)


def test_parser_rejects_undefined_call():
    with pytest.raises(AnalysisError, match="undefined function"):
        analyze_code("f();")


def test_parser_rejects_wrong_arity():
    with pytest.raises(AnalysisError, match="expects 1 argument"):
        analyze_code("sink();")
    with pytest.raises(AnalysisError, match="expects 2 argument"):
        analyze_code("func f(a, b) { return a; } f(1);")


def test_undeclared_variable_read_is_error():
    with pytest.raises(AnalysisError, match="undeclared variable"):
        analyze_code("sink(x);")


# --------------------------------------------------------------------------- #
# direct taint flows
# --------------------------------------------------------------------------- #


def test_direct_source_to_sink_is_vulnerable():
    res = analyze_code("x = source();\nsink(x);\n")
    assert res["vulnerable"] is True
    f = res["findings"][0]
    kinds = [s["kind"] for s in f["path"]]
    assert kinds == ["source", "assign", "sink"]
    assert f["source"]["loc"].startswith("1:")
    assert f["sink"]["loc"].startswith("2:")


def test_clean_constant_is_safe():
    res = analyze_code("sink(1 + 2);")
    assert res["vulnerable"] is False
    assert res["findings"] == []


def test_sanitize_kills_taint():
    res = analyze_code("x = source(); x = sanitize(x); sink(x);")
    assert res["vulnerable"] is False


def test_taint_propagates_through_binary_ops():
    res = analyze_code("x = source(); y = x + 1; z = y * 2; sink(z);")
    assert res["vulnerable"] is True
    assert [s["kind"] for s in res["findings"][0]["path"]] == [
        "source", "assign", "op", "assign", "op", "assign", "sink"]


# --------------------------------------------------------------------------- #
# branches: the false-positive boundary
# --------------------------------------------------------------------------- #


def test_partial_branch_sanitization_is_reported_may_analysis():
    code = (FIXTURES / "conditional_sanitize_fp.tl").read_text()
    res = analyze_code(code)
    # one arm still flows tainted -> may analysis reports it
    assert res["vulnerable"] is True


def test_all_branch_arms_sanitized_is_safe():
    code = (FIXTURES / "conditional_sanitize_safe.tl").read_text()
    res = analyze_code(code)
    assert res["vulnerable"] is False


# --------------------------------------------------------------------------- #
# loops & fixpoint
# --------------------------------------------------------------------------- #


def test_loop_carried_taint_requires_fixpoint():
    code = (FIXTURES / "loop_fixpoint.tl").read_text()
    res = analyze_code(code)
    assert res["vulnerable"] is True


# --------------------------------------------------------------------------- #
# cross-function propagation
# --------------------------------------------------------------------------- #


def test_cross_function_propagation_fixture():
    code = (FIXTURES / "cross_function.tl").read_text()
    res = analyze_code(code)
    assert res["vulnerable"] is True
    f = res["findings"][0]
    kinds = [s["kind"] for s in f["path"]]
    assert "call" in kinds
    assert f["call_chain"][0] == "forward"
    assert "sanitize_like" in f["call_chain"]
    assert f["call_chain"][-1] == "<main>::sink"
    # evidence path runs from source in main through both calls to sink
    assert kinds[0] == "source"
    assert kinds[-1] == "sink"
    # call steps record the function containing each call
    funcs = [s["func"] for s in f["path"] if s["kind"] == "call"]
    assert funcs == ["<main>", "forward"]


def test_source_inside_callee_reaches_top_level_sink():
    code = (FIXTURES / "source_in_callee.tl").read_text()
    res = analyze_code(code)
    assert res["vulnerable"] is True
    f = res["findings"][0]
    assert f["source"]["func"] == "get_input"
    assert f["sink"]["func"] == "<main>"


def test_safe_fixture_has_no_findings():
    code = (FIXTURES / "safe.tl").read_text()
    res = analyze_code(code)
    assert res["vulnerable"] is False


def test_return_only_known_clean_is_safe():
    # function parameter never tainted on any call site: no finding
    code = (
        "func id(x) { return x; }\n"
        "a = id(1);\n"
        "sink(a);\n")
    res = analyze_code(code)
    assert res["vulnerable"] is False


def test_same_function_called_clean_and_dirty():
    code = (
        "func id(x) { return x; }\n"
        "a = id(1);\n"
        "b = id(source());\n"
        "sink(a);\n"
        "sink(b);\n")
    res = analyze_code(code)
    # only the dirty call site produces a finding; clean site stays clean
    assert len(res["findings"]) == 1
    assert res["findings"][0]["sink"]["loc"].startswith("5:")


# --------------------------------------------------------------------------- #
# recursion
# --------------------------------------------------------------------------- #


def test_mutual_recursion_converges_and_reports():
    code = (FIXTURES / "recursion.tl").read_text()
    res = analyze_code(code)
    assert res["stats"]["scc_fixpoint_rounds"] >= 2
    assert res["vulnerable"] is True
    f = res["findings"][0]
    assert f["path"][0]["kind"] == "source"
    assert f["path"][-1]["kind"] == "sink"
    # shortest path crosses the recursive SCC: unroll -> step -> sink
    assert "unroll" in f["call_chain"]
    assert "step" in f["call_chain"]
    assert f["call_chain"][-1] == "step::sink"


def test_direct_recursion_converges():
    code = (
        "func count(n, d) {\n"
        "  if (n <= 0) { sink(d); return 0; }\n"
        "  return count(n - 1, d);\n"
        "}\n"
        "v = source(); count(5, v);\n")
    res = analyze_code(code)
    assert res["vulnerable"] is True


def test_recursion_with_clean_argument_is_safe():
    code = (
        "func count(n, d) {\n"
        "  if (n <= 0) { sink(d); return 0; }\n"
        "  return count(n - 1, d);\n"
        "}\n"
        "count(5, 0);\n")
    res = analyze_code(code)
    assert res["vulnerable"] is False


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #


def test_health_endpoint():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_analyze_endpoint_dangerous_signed():
    code = (FIXTURES / "cross_function.tl").read_text()
    r = client.post("/analyze", json={"code": code})
    assert r.status_code == 200
    body = r.json()
    report, signature = body["report"], body["signature"]
    assert report["ok"] is True
    assert report["vulnerable"] is True
    pem = client.get("/public-key").text.encode()
    assert verify(pem, signature, report) is True
    # tampering invalidates the signature
    tampered = json.loads(json.dumps(report))
    tampered["vulnerable"] = False
    assert verify(pem, signature, tampered) is False


def test_analyze_endpoint_safe():
    code = (FIXTURES / "safe.tl").read_text()
    r = client.post("/analyze", json={"code": code})
    report = r.json()["report"]
    assert report["vulnerable"] is False
    assert "context_sensitivity" in report["analysis"]


def test_analyze_endpoint_parse_error():
    r = client.post("/analyze", json={"code": "x = ;"})
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["type"] in ("LexError", "ParseError")


def test_verify_signature_endpoint():
    r = client.post("/analyze", json={"code": "x = source(); sink(x);"})
    body = r.json()
    ok = client.post("/verify-signature",
                     json={"report": body["report"], "signature": body["signature"]})
    assert ok.json()["valid"] is True
    bad = client.post("/verify-signature",
                      json={"report": body["report"], "signature": "AAAA"})
    assert bad.json()["valid"] is False
