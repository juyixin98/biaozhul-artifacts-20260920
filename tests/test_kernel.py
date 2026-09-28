"""内核测试：逐项对照成熟排序库（PyICU），并覆盖重音、数字片段、
大小写、土耳其字符、规范等价与排序稳定性。

参考答案来源：测试内独立构建的 icu.Collator（对照库）与冻结夹具，
不由被测内核自身生成。
"""
from __future__ import annotations

import icu
import pytest

from app.config import CollationRules
from app.corpus import CorpusEntry
from app.kernel import CollationKernel

from .conftest import build_reference_collator


def test_sort_keys_match_library_itemwise(kernel, sample_entries):
    """逐条比较：内核排序键必须与对照库逐字节一致。"""
    reference = build_reference_collator(kernel.rules)
    mismatches = [
        entry.id
        for entry in sample_entries
        if kernel.sort_key(entry.text) != reference.getSortKey(entry.text)
    ]
    assert mismatches == [], f"排序键与对照库不一致: {mismatches}"


def test_total_order_matches_library(kernel, sample_entries):
    """整体顺序与对照库排序 + seq 决胜一致。"""
    reference = build_reference_collator(kernel.rules)
    expected = sorted(
        sample_entries, key=lambda e: (reference.getSortKey(e.text), e.seq)
    )
    actual = [entry for entry, _ in kernel.sort_entries(sample_entries)]
    assert [e.id for e in actual] == [e.id for e in expected]


def test_numeric_collation_on_orders_digit_segments():
    kernel = CollationKernel(CollationRules(numeric=True))
    assert kernel.sort_key("file2") < kernel.sort_key("file10")


def test_numeric_collation_off_orders_lexically():
    kernel = CollationKernel(CollationRules(numeric=False))
    assert kernel.sort_key("file10") < kernel.sort_key("file2")


def test_accent_ignored_at_primary_strength():
    kernel = CollationKernel(CollationRules(strength="primary"))
    assert kernel.sort_key("cote") == kernel.sort_key("côte")


def test_accent_distinguished_at_tertiary_strength(kernel):
    reference = build_reference_collator(kernel.rules)
    # 与对照库逐项一致。
    for a, b in [("cote", "coté"), ("coté", "côte"), ("côte", "côté")]:
        assert (kernel.sort_key(a) < kernel.sort_key(b)) == (
            reference.getSortKey(a) < reference.getSortKey(b)
        )
    # 冻结断言（ICU 语义：最早出现重音差异的位置主导二级比较）。
    assert kernel.sort_key("cote") < kernel.sort_key("coté")
    assert kernel.sort_key("coté") < kernel.sort_key("côte")


def test_case_first_upper_sorts_capital_first():
    kernel = CollationKernel(CollationRules(case_first="upper_first"))
    assert kernel.sort_key("Apple") < kernel.sort_key("apple")


def test_case_first_lower_sorts_lowercase_first():
    kernel = CollationKernel(CollationRules(case_first="lower_first"))
    assert kernel.sort_key("apple") < kernel.sort_key("Apple")


def test_turkish_locale_orders_dotted_and_dotless_i():
    """土耳其语序：ı < I < i < İ（对照库逐项验证，非内核自证）。"""
    kernel = CollationKernel(CollationRules(locale="tr_TR"))
    reference = icu.Collator.createInstance(icu.Locale("tr_TR"))
    letters = ["i", "I", "ı", "İ"]
    expected = sorted(letters, key=reference.getSortKey)
    actual = sorted(letters, key=kernel.sort_key)
    assert actual == expected
    # 冻结断言：土耳其语中 dotless ı 排在 i 之前，与英语相反。
    assert actual == ["ı", "I", "i", "İ"]
    en = CollationKernel(CollationRules(locale="en_US"))
    assert sorted(letters, key=en.sort_key) != actual


def test_canonical_equivalents_share_sort_key(kernel):
    nfc = "café"
    nfd = "café"
    assert nfc != nfd  # 原文不同
    assert kernel.sort_key(nfc) == kernel.sort_key(nfd)  # 排序键相同


def test_sort_is_stable_for_equal_keys(kernel):
    """排序键相等时按载入顺序决胜，且每个身份都保留。"""
    entries = [
        CorpusEntry(id="first", text="café", seq=0),
        CorpusEntry(id="second", text="café", seq=1),
        CorpusEntry(id="third", text="CAFÉ", seq=2),
    ]
    ordered = kernel.sort_entries(entries)
    assert [e.id for e, _ in ordered] == ["first", "second", "third"]
    # 重复调用结果一致（确定性）。
    again = kernel.sort_entries(list(reversed(entries)))
    assert [e.id for e, _ in again] == ["first", "second", "third"]


def test_index_version_binds_rules_and_icu():
    """规则任一维度变化都必须改变索引版本。"""
    base = CollationKernel(CollationRules())
    variants = [
        CollationRules(locale="tr_TR"),
        CollationRules(strength="primary"),
        CollationRules(numeric=True),
        CollationRules(case_first="upper_first"),
    ]
    for rules in variants:
        other = CollationKernel(rules)
        assert other.index_version != base.index_version, f"规则变化未改变版本: {rules}"
    same = CollationKernel(CollationRules())
    assert same.index_version == base.index_version
    assert base.index_version.startswith(f"icu{icu.ICU_VERSION}-")
