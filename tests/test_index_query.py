"""SQLite 持久化、冷启动重建、查询边界校验测试。"""

from __future__ import annotations

import pytest

from wfst.config import Settings
from wfst.index.store import Store
from wfst.manager import ModelManager
from wfst.query import (
    ErrorCategory,
    QueryError,
    run_query,
    tokenize_for_model,
)

FIX = "fixtures/corpora/char_morph_demo.json"


def test_ingest_persists_and_rebuilds_from_store(settings):
    store1 = Store(settings.db_path)
    mgr1 = ModelManager(store1, settings)
    ingested = mgr1.ingest_file(FIX)
    before = [
        (h.output, round(h.cost, 6))
        for h in run_query(ingested.model, "kat", k=5).hypotheses
    ]
    store1.close()

    # 全新管理器（空内存）从 SQLite 重建同一模型，结果必须一致。
    store2 = Store(settings.db_path)
    mgr2 = ModelManager(store2, settings)
    rebuilt = mgr2.load_from_store("char_morph_demo", "1.0.0")
    after = [
        (h.output, round(h.cost, 6))
        for h in run_query(rebuilt, "kat", k=5).hypotheses
    ]
    assert before == after
    assert rebuilt.pipeline.num_states == ingested.model.pipeline.num_states
    store2.close()


def test_mined_arcs_persisted(settings):
    store = Store(settings.db_path)
    mgr = ModelManager(store, settings)
    mgr.ingest_file(FIX)
    arcs = store.get_mined_arcs("char_morph_demo", "1.0.0")
    ops = {row["op"] for row in arcs}
    assert {"sub", "del", "identity"} & ops
    assert all(row["cost"] >= 0 for row in arcs)
    store.close()


def test_get_unknown_model_raises_distinct_category(settings):
    store = Store(settings.db_path)
    mgr = ModelManager(store, settings)
    with pytest.raises(QueryError) as exc:
        mgr.get("nope")
    assert exc.value.category is ErrorCategory.MODEL_NOT_FOUND
    store.close()


def test_empty_input_rejected(char_model):
    with pytest.raises(QueryError) as exc:
        run_query(char_model, "", k=3)
    assert exc.value.category is ErrorCategory.EMPTY_INPUT


def test_too_long_input_rejected(char_model):
    with pytest.raises(QueryError) as exc:
        run_query(char_model, "c" * 100, k=3, max_input_len=8)
    assert exc.value.category is ErrorCategory.INPUT_TOO_LONG
    assert exc.value.details["max"] == 8


def test_unknown_symbol_rejected_with_details(char_model):
    with pytest.raises(QueryError) as exc:
        run_query(char_model, "qv", k=3)  # q、v 均不在夹具字母表
    assert exc.value.category is ErrorCategory.UNKNOWN_SYMBOL
    assert set(exc.value.details["unknown"]) == {"q", "v"}


def test_invalid_parameters_rejected(char_model):
    with pytest.raises(QueryError) as exc:
        run_query(char_model, "cat", k=0)
    assert exc.value.category is ErrorCategory.INVALID_PARAMETER
    with pytest.raises(QueryError) as exc:
        run_query(char_model, "cat", budget=0)
    assert exc.value.category is ErrorCategory.INVALID_PARAMETER


def test_word_tokenization_adds_boundary(word_model):
    toks = tokenize_for_model("walk slowly", word_model)
    assert toks == ["walk\x00", "slowly\x00"]
