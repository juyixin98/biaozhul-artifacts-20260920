"""挖掘内核测试：独立 Levenshtein 脚本、手算平滑代价、标签隔离。

这些期望值由人工按 -ln((c+α)/(Σc+α·K)) 计算，不调用被测核心算法。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from wfst.corpus.mining import EPSILON, Op, levenshtein_alignment, mine_corpus
from wfst.corpus.spec import parse_corpus


def test_levenshtein_script_substitution():
    script = levenshtein_alignment("kat", "cat")
    assert script == [
        (Op.SUB, "k", "c"),
        (Op.IDENTITY, "a", "a"),
        (Op.IDENTITY, "t", "t"),
    ]


def test_levenshtein_script_deletion_and_insertion_use_separate_epsilon():
    # 删除：olabel 为 epsilon（输出 epsilon）。
    assert levenshtein_alignment("ab", "b") == [
        (Op.DEL, "a", EPSILON),
        (Op.IDENTITY, "b", "b"),
    ]
    # 插入：ilabel 为 epsilon（输入 epsilon）——与删除是不同侧。
    assert levenshtein_alignment("b", "ab") == [
        (Op.INS, EPSILON, "a"),
        (Op.IDENTITY, "b", "b"),
    ]


def test_levenshtein_tie_break_is_deterministic():
    # 并列最优时两次调用结果必须完全一致（稳定回溯）。
    a = levenshtein_alignment("abc", "xacb")
    b = levenshtein_alignment("abc", "xacb")
    assert a == b


def _two_alternative_corpus():
    return parse_corpus({
        "corpus_id": "calc",
        "version": "1.0.0",
        "token_level": "char",
        "edit_tags": ["spell"],
        "alignments": [
            {"input": "ab", "output": "ac", "count": 4, "tag": "spell"},
            {"input": "ab", "output": "ad", "count": 1, "tag": "spell"},
            {"input": "ab", "output": "AB", "count": 10, "tag": "lex"},
            {"input": "ab", "output": "ab", "count": 6, "tag": "lex"},
        ],
    })


def test_substitution_costs_hand_computed():
    report = mine_corpus(_two_alternative_corpus(), smoothing=0.5)
    by_pair = {(a.ilabel, a.olabel): a.cost for a in report.arcs if a.op is Op.SUB}
    # sub 组：(b,c)=4, (b,d)=1，总数 5，备选 K=2，α=0.5。
    assert by_pair[("b", "c")] == pytest_approx(-math.log(4.5 / 6.0))
    assert by_pair[("b", "d")] == pytest_approx(-math.log(1.5 / 6.0))
    # 高频替换代价必须更低。
    assert by_pair[("b", "c")] < by_pair[("b", "d")]


def pytest_approx(v):
    import pytest

    return pytest.approx(v, abs=1e-9)


def test_lexicon_costs_hand_computed_and_isolated_from_spell():
    report = mine_corpus(_two_alternative_corpus(), smoothing=0.5)
    lex = {(e.text_in, e.text_out): e.cost for e in report.lexicon_entries}
    # 词典仅来自非 spell 标签：("ab","AB")=10, ("ab","ab")=6，总数 16，K=2。
    assert lex[("ab", "AB")] == pytest_approx(-math.log(10.5 / 17.0))
    assert lex[("ab", "ab")] == pytest_approx(-math.log(6.5 / 17.0))
    # spell 对（ab->ac / ab->ad）不得进入词典。
    assert ("ab", "ac") not in lex and ("ab", "ad") not in lex


def test_explicit_rules_not_mined_into_edit_arcs():
    # char_morph_demo 显式声明了 y->i 规则；挖掘弧中不应混入该规则
    # （它属于独立的 rules 级，避免泄漏到 edit 级）。
    raw = json.loads(
        Path("fixtures/corpora/char_morph_demo.json").read_text(encoding="utf-8")
    )
    report = mine_corpus(parse_corpus(raw), smoothing=0.5)
    sub_pairs = {(a.ilabel, a.olabel) for a in report.arcs if a.op is Op.SUB}
    # 挖掘出的替换来自拼写对；y->i 不在挖掘结果中（来自显式规则）。
    assert ("y", "i") not in sub_pairs


def test_mining_report_steps_contain_counts():
    report = mine_corpus(_two_alternative_corpus(), smoothing=0.5)
    steps = "\n".join(report.steps())
    assert "sub" in steps
    assert "α=0.5" in steps
