"""FastAPI HTTP 层测试：响应信封、错误类别状态码、预算未完成、日志关联。"""

from __future__ import annotations

import json


def test_health_reports_version(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["status"] == "ok"
    assert body["data"]["service_version"]


def test_models_lists_ingested(client):
    r = client.get("/models")
    ids = {m["corpus_id"] for m in r.json()["data"]["models"]}
    assert "char_morph_demo" in ids
    assert "epsilon_ambiguity_demo" in ids


def test_query_success_envelope_and_order(client):
    r = client.post("/query", json={
        "corpus_id": "char_morph_demo", "input": "kat", "k": 5,
    })
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "complete" and data["complete"] is True
    outs = [h["output"] for h in data["hypotheses"]]
    assert outs[0] == "cat"            # 具体结果断言
    assert "cats" in outs
    costs = [h["cost"] for h in data["hypotheses"]]
    assert costs == sorted(costs)      # 代价升序
    assert data["trace"]               # 含计算步骤


def test_query_unknown_model_404(client):
    r = client.post("/query", json={"corpus_id": "ghost", "input": "a"})
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["category"] == "model_not_found"
    assert r.json()["success"] is False


def test_query_validation_error_422_with_category(client):
    r = client.post("/query", json={
        "corpus_id": "char_morph_demo", "input": "qz",
    })
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "unknown_symbol"


def test_query_empty_input_422(client):
    r = client.post("/query", json={"corpus_id": "char_morph_demo", "input": ""})
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "empty_input"


def test_budget_exhausted_marks_incomplete_but_success(client):
    # 极小预算 + 含插入自环的模型：结果被截断，success=true 但 status=incomplete。
    r = client.post("/query", json={
        "corpus_id": "epsilon_ambiguity_demo", "input": "ab",
        "k": 100, "budget": 8,
    })
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["complete"] is False
    assert data["status"] == "incomplete"
    assert len(data["hypotheses"]) > 0   # 仍返回已找到的部分结果


def test_cross_check_agrees(client):
    r = client.post("/query/cross-check", json={
        "corpus_id": "char_morph_demo", "input": "citi", "k": 8,
    })
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["agree"] is True
    c = [h["output"] for h in data["composed"]["hypotheses"]]
    s = [h["output"] for h in data["stagewise"]["hypotheses"]]
    assert c == s


def test_cross_check_mismatch_returns_409(client, monkeypatch):
    # 人为让顺序执行的代价与组合不同，验证不一致时明确返回 409 而非成功。
    import importlib

    app_mod = importlib.import_module("wfst.service.app")
    from wfst.algorithms.shortest_paths import Hypothesis

    original = app_mod.transduce_stagewise

    def tampered(model, text, **kwargs):
        resp = original(model, text, **kwargs)
        resp.hypotheses = [
            Hypothesis(output=h.output, cost=h.cost + 99.0)
            for h in resp.hypotheses
        ]
        return resp

    monkeypatch.setattr(app_mod, "transduce_stagewise", tampered)
    r = client.post("/query/cross-check", json={
        "corpus_id": "char_morph_demo", "input": "kat", "k": 5,
    })
    assert r.status_code == 409
    body = r.json()
    assert body["success"] is False
    assert body["error"]["category"] == "cross_check_mismatch"


def test_ingest_invalid_corpus_422_lists_errors(client, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"corpus_id": "", "version": "x", "alignments": []}',
                   encoding="utf-8")
    r = client.post("/models/ingest", json={"path": str(bad)})
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "corpus_validation"
    assert r.json()["error"]["errors"]   # 明确列出失败项


def test_ingest_missing_file_422(client):
    r = client.post("/models/ingest", json={"path": "/nonexistent/nope.json"})
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "invalid_parameter"


def test_run_log_correlates_run_id_and_verdict(client, settings):
    r = client.post("/query", json={
        "corpus_id": "char_morph_demo", "input": "kat", "k": 3,
    })
    run_id = r.json()["data"]["run_id"]
    lines = (settings.log_dir / "runs.jsonl").read_text(encoding="utf-8").strip().splitlines()
    records = [json.loads(line) for line in lines]
    mine = [rec for rec in records if rec.get("run_id") == run_id and rec["event"] == "query_ok"]
    assert mine, "成功查询必须落一条可按 run_id 关联的日志"
    rec = mine[-1]
    assert rec["input"] == "kat"
    assert rec["service_version"]
    assert rec["corpus"].startswith("char_morph_demo@")
    assert rec["verdict"] == "ok"
    assert isinstance(rec["steps"], list) and rec["steps"]


def test_error_log_is_not_marked_success(client, settings):
    client.post("/query", json={"corpus_id": "char_morph_demo", "input": "qz"})
    lines = (settings.log_dir / "runs.jsonl").read_text(encoding="utf-8").strip().splitlines()
    verdicts = [json.loads(line).get("verdict", "") for line in lines]
    assert any(v.startswith("error:") for v in verdicts)
    assert not any(v == "success" for v in verdicts if v.startswith("error"))
