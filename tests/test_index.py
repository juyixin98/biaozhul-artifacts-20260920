"""索引测试：版本管理、稳定全序落库、范围边界使用排序键。"""
from __future__ import annotations

import pytest

from app.config import CollationRules
from app.corpus import CorpusEntry
from app.errors import ErrorCategory, ServiceError
from app.kernel import CollationKernel


def test_require_version_before_build(index, kernel):
    with pytest.raises(ServiceError) as excinfo:
        index.require_version(kernel)
    assert excinfo.value.category == ErrorCategory.INDEX_NOT_BUILT


def test_rebuild_stores_version_and_entries(index, kernel, sample_entries):
    count = index.rebuild(kernel, sample_entries)
    assert count == len(sample_entries)
    assert index.stored_version() == kernel.index_version
    assert index.require_version(kernel) == kernel.index_version
    assert index.count() == len(sample_entries)


def test_rule_upgrade_requires_rebuild(index, sample_entries):
    old_kernel = CollationKernel(CollationRules(numeric=False))
    index.rebuild(old_kernel, sample_entries)
    new_kernel = CollationKernel(CollationRules(numeric=True))
    with pytest.raises(ServiceError) as excinfo:
        index.require_version(new_kernel)
    assert excinfo.value.category == ErrorCategory.INDEX_VERSION_CONFLICT
    assert excinfo.value.detail["stored_version"] == old_kernel.index_version
    assert excinfo.value.detail["kernel_version"] == new_kernel.index_version


def test_sorted_iteration_is_stable_for_equal_keys(index, kernel):
    entries = [
        CorpusEntry(id="nfc", text="café", seq=0),
        CorpusEntry(id="nfd", text="café", seq=1),
    ]
    index.rebuild(kernel, entries)
    rows = index.fetch_page(None, 10)
    assert [r["id"] for r in rows] == ["nfc", "nfd"]
    # 排序键与原文同时保存。
    assert rows[0]["sort_key"] == rows[1]["sort_key"]
    assert rows[0]["original"] != rows[1]["original"]


def test_range_boundaries_use_sort_keys_not_utf8(index, kernel, sample_entries):
    """'é' 在 UTF-8 下大于 'z'，在 en_US 排序下小于 'z'。

    若实现误用 UTF-8 比较，lower='é'/upper='z' 会是空区间或反向区间；
    正确实现必须返回主字母落在 é..z 之间的条目（file*、istanbul 等），
    而以 'c' 开头的 café/cote 即使含 é 也在区间之外。
    """
    assert "é".encode("utf-8") > "z".encode("utf-8")  # 前提：UTF-8 序相反
    index.rebuild(kernel, sample_entries)
    rows = index.fetch_range(
        kernel.sort_key("é"), kernel.sort_key("z"),
        lower_inclusive=True, upper_inclusive=True, limit=100,
    )
    ids = {r["id"] for r in rows}
    assert ids  # UTF-8 实现会给出空集
    assert {"num-file1", "num-file2", "tr-istanbul", "de-mueller-ascii"} <= ids
    assert "accent-cote" not in ids      # 'c' 在 é 之前
    assert "canon-cafe-nfc" not in ids   # café 以 'c' 开头，含 é 也不在区间内


def test_range_exclusivity(index, kernel):
    entries = [
        CorpusEntry(id="a", text="alpha", seq=0),
        CorpusEntry(id="b", text="beta", seq=1),
        CorpusEntry(id="g", text="gamma", seq=2),
    ]
    index.rebuild(kernel, entries)
    rows = index.fetch_range(
        kernel.sort_key("alpha"), kernel.sort_key("gamma"),
        lower_inclusive=False, upper_inclusive=False, limit=10,
    )
    assert [r["id"] for r in rows] == ["b"]
