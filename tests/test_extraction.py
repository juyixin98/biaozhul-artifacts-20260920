"""Tests for the explainable extraction engine: offsets, alias preservation."""
from __future__ import annotations

import pytest

from kex.extraction import (
    ENTITY_TYPES,
    RuleValidationError,
    builtin_snapshot,
    canonicalize_snapshot,
    compile_snapshot,
    serialize_snapshot,
)


@pytest.fixture()
def rules():
    return compile_snapshot(serialize_snapshot(builtin_snapshot()))


def test_four_entity_types_present(rules):
    text = (
        "李明与 Alice Chen 在 2023-05-01 讨论 Kafka 3.6，"
        "Acme Corp 的 Bob 用 PostgreSQL 做存储。"
    )
    mentions = rules.extract(text)
    by_type = {m.entity_type for m in mentions}
    assert {"PERSON", "ORG", "TECH", "DATE"} <= by_type


def test_offsets_slice_back_to_surface(rules):
    text = "二〇二五年十二月九日，联合国发布公告。"
    mentions = rules.extract(text)
    date = next(m for m in mentions if m.entity_type == "DATE")
    assert text[date.start_char:date.end_char] == date.text == "二〇二五年十二月九日"
    assert date.canonical_name == "2025-12-09"
    assert date.matched_rule == "date.cn"


def test_alias_normalization_keeps_original_mention(rules):
    # "小李" is an alias of canonical "李明"; original text must survive.
    mentions = rules.extract("小李主持了会议，明哥做记录。")
    texts = [(m.text, m.canonical_name) for m in mentions]
    assert ("小李", "李明") in texts
    assert ("明哥", "李明") in texts


def test_english_date_forms(rules):
    text = "Signed on September 9, 2024 and also 2024/09/10."
    mentions = rules.extract(text)
    dates = [m for m in mentions if m.entity_type == "DATE"]
    canon = {m.canonical_name for m in dates}
    assert "2024-09-09" in canon
    assert "2024-09-10" in canon


def test_longest_match_prefers_versioned_pattern(rules):
    mentions = rules.extract("we run Kafka 3.6 in prod")
    techs = [m for m in mentions if m.entity_type == "TECH"]
    assert any(m.text == "Kafka 3.6" for m in techs)
    # The shorter dictionary hit must be suppressed at the same span.
    assert not any(m.text == "Kafka" for m in techs)


def test_overlapping_dictionary_entries_do_not_double_emit(rules):
    mentions = rules.extract("联合国和United Nations是同一组织")
    canon = {m.canonical_name for m in mentions if m.entity_type == "ORG"}
    assert canon == {"联合国"}


def test_boundary_guards_prevent_substring_fires(rules):
    # "EU"-like ascii entries must not fire inside a larger word; "may" is
    # a month name but appears inside ordinary prose here.
    mentions = rules.extract("the mayor may open source everything")
    assert not [m for m in mentions if m.entity_type == "DATE"]


def test_invalid_date_not_emitted(rules):
    mentions = rules.extract("日期 2023-13-40 是非法的")
    assert not [m for m in mentions if m.entity_type == "DATE"]


def test_custom_snapshot_is_dynamic_not_canned():
    snapshot = canonicalize_snapshot(
        {
            "gazetteer": {
                "PERSON": [{"canonical": "诺依曼", "aliases": ["冯诺依曼", "Neumann"]}],
            }
        }
    )
    rules = compile_snapshot(snapshot)
    mentions = rules.extract("冯诺依曼与Neumann提出了架构")
    assert {m.canonical_name for m in mentions} == {"诺依曼"}
    assert {m.text for m in mentions} == {"冯诺依曼", "Neumann"}


def test_duplicate_alias_mapping_rejected():
    with pytest.raises(RuleValidationError):
        canonicalize_snapshot(
            {
                "gazetteer": {
                    "TECH": [
                        {"canonical": "A", "aliases": ["shared"]},
                        {"canonical": "B", "aliases": ["shared"]},
                    ]
                }
            }
        )


def test_bad_regex_rejected():
    with pytest.raises(RuleValidationError):
        canonicalize_snapshot({"tech_patterns": [{"pattern": "(unclosed"}]})


def test_results_vary_with_input(rules):
    """Sanity: the engine must not return fixed output."""
    a = rules.extract("李明在 2020-01-01 出生")
    b = rules.extract("王芳在 2022-02-02 毕业")
    assert [m.text for m in a] != [m.text for m in b]


def test_entity_types_constant():
    assert set(ENTITY_TYPES) == {"PERSON", "ORG", "TECH", "DATE"}
