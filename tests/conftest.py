"""测试公共夹具。

参考实现（对照库）是测试内独立构建的 icu.Collator —— 即成熟排序库本身，
而不是被测服务内核；冻结参考顺序见 fixtures/reference_orderings.json。
"""
from __future__ import annotations

import json
from pathlib import Path

import icu
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import CollationRules
from app.corpus import CorpusEntry, load_corpus
from app.index import SQLiteIndex
from app.kernel import CollationKernel

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_CORPUS = REPO_ROOT / "data" / "sample_corpus.json"
REFERENCE_FIXTURE = Path(__file__).parent / "fixtures" / "reference_orderings.json"


def build_reference_collator(rules: CollationRules) -> icu.Collator:
    """在测试侧独立构建的对照库实例（不经过被测内核）。"""
    collator = icu.Collator.createInstance(icu.Locale(rules.locale))
    collator.setStrength(
        {
            "primary": icu.Collator.PRIMARY,
            "secondary": icu.Collator.SECONDARY,
            "tertiary": icu.Collator.TERTIARY,
            "quaternary": icu.Collator.QUATERNARY,
            "identical": icu.Collator.IDENTICAL,
        }[rules.strength]
    )
    collator.setAttribute(
        icu.UCollAttribute.NUMERIC_COLLATION,
        icu.UCollAttributeValue.ON if rules.numeric else icu.UCollAttributeValue.OFF,
    )
    if rules.case_first == "upper_first":
        collator.setAttribute(
            icu.UCollAttribute.CASE_FIRST, icu.UCollAttributeValue.UPPER_FIRST
        )
    elif rules.case_first == "lower_first":
        collator.setAttribute(
            icu.UCollAttribute.CASE_FIRST, icu.UCollAttributeValue.LOWER_FIRST
        )
    return collator


@pytest.fixture()
def rules() -> CollationRules:
    return CollationRules()


@pytest.fixture()
def kernel(rules) -> CollationKernel:
    return CollationKernel(rules)


@pytest.fixture()
def sample_entries():
    return load_corpus(SAMPLE_CORPUS)


@pytest.fixture()
def index(tmp_path) -> SQLiteIndex:
    idx = SQLiteIndex(tmp_path / "test.db")
    yield idx
    idx.close()


@pytest.fixture()
def make_client(tmp_path):
    """按给定规则创建已启动的 TestClient（共用 tmp 目录下独立 db）。"""
    counter = {"n": 0}

    def factory(rules: CollationRules, db_name: str | None = None):
        counter["n"] += 1
        db = tmp_path / (db_name or f"api-{counter['n']}.db")
        app = create_app(rules, db_path=str(db), corpus_path=str(SAMPLE_CORPUS))
        return TestClient(app)

    return factory


@pytest.fixture()
def client(make_client) -> TestClient:
    with make_client(CollationRules()) as c:
        yield c


def reference_fixture() -> dict:
    return json.loads(REFERENCE_FIXTURE.read_text(encoding="utf-8"))
