"""语料规范校验测试：明确的失败类别与多错误聚合。"""

from __future__ import annotations

import json

import pytest

from wfst.corpus.spec import (
    CorpusValidationError,
    corpus_fingerprint,
    parse_corpus,
)


def _valid_raw():
    return {
        "corpus_id": "c",
        "version": "1.0.0",
        "token_level": "char",
        "alignments": [{"input": "ab", "output": "ac", "count": 3, "tag": "spell"}],
        "rules": [],
    }


def test_valid_corpus_parses():
    spec = parse_corpus(_valid_raw())
    assert spec.corpus_id == "c"
    assert spec.alignments[0].count == 3


def test_missing_fields_aggregate_all_errors():
    raw = {"corpus_id": "", "version": "bad", "alignments": []}
    with pytest.raises(CorpusValidationError) as exc:
        parse_corpus(raw)
    joined = " ".join(exc.value.errors)
    assert "corpus_id" in joined
    assert "version" in joined
    # alignments 为空也是一条独立错误（不止报第一个）。
    assert "alignments" in joined
    assert len(exc.value.errors) >= 3


def test_epsilon_token_rejected():
    raw = _valid_raw()
    raw["alignments"][0]["input"] = "a<eps>b"
    with pytest.raises(CorpusValidationError):
        parse_corpus(raw)


def test_rule_kind_requires_labels():
    raw = _valid_raw()
    raw["rules"] = [{"ilabel": "", "olabel": "", "kind": "sub"}]
    with pytest.raises(CorpusValidationError) as exc:
        parse_corpus(raw)
    assert any("sub" in e for e in exc.value.errors)


def test_del_rule_allows_empty_olabel():
    raw = _valid_raw()
    raw["rules"] = [{"ilabel": "e", "olabel": "", "kind": "del", "weight_hint": 0.9}]
    spec = parse_corpus(raw)
    assert spec.rules[0].kind == "del"


def test_non_finite_weight_rejected():
    with pytest.raises(CorpusValidationError):
        parse_corpus({
            **_valid_raw(),
            "rules": [{"ilabel": "a", "olabel": "b", "weight_hint": float("inf")}],
        })


def test_bad_json_file_reports_distinct_category(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    from wfst.corpus.spec import load_corpus

    with pytest.raises(CorpusValidationError) as exc:
        load_corpus(p)
    assert any("JSON" in e for e in exc.value.errors)


def test_fingerprint_stable_and_distinct():
    a = json.dumps(_valid_raw(), ensure_ascii=False).encode("utf-8")
    b = json.dumps({**_valid_raw(), "version": "1.0.1"}, ensure_ascii=False).encode("utf-8")
    assert corpus_fingerprint(a) == corpus_fingerprint(a)
    assert corpus_fingerprint(a) != corpus_fingerprint(b)
