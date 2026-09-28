"""构建器直接测试（不经完整管线）。"""

from __future__ import annotations

from wfst.algorithms.compose import compose
from wfst.algorithms.shortest_paths import nbest_paths
from wfst.builders import (
    chain_acceptor,
    edit_cascade_fst,
    lexicon_fst,
    rewrite_rule_fst,
)
from wfst.core.fst import EPSILON


def test_chain_acceptor_only_accepts_exact_sequence():
    chain = chain_acceptor(["a", "b"])
    assert chain.num_states == 3 and chain.is_final(2)
    assert nbest_paths(compose(chain, chain_acceptor(["a", "b"])), k=2).hypotheses[0].output == "ab"


def test_lexicon_sequence_form_multiple_outputs():
    lex = lexicon_fst([
        ("ab", "AB", 0.1),
        ("ab", "ab", 0.4),
    ], name="lex")
    chained = compose(chain_acceptor(["a", "b"]), lex)
    res = nbest_paths(chained, k=5)
    assert [(h.output, round(h.cost, 3)) for h in res.hypotheses] == [
        ("AB", 0.1), ("ab", 0.4)
    ]


def test_lexicon_mapping_forms():
    # {in: {out: weight}} 多输出形式。
    lex = lexicon_fst({"x": {"X": 0.2, "Y": 0.7}}, name="lex")
    chained = compose(chain_acceptor(["x"]), lex)
    assert [h.output for h in nbest_paths(chained, k=3).hypotheses] == ["X", "Y"]
    # {in: weight} 恒等词典形式。
    ident = lexicon_fst({"z": 0.3}, name="ident")
    out = nbest_paths(compose(chain_acceptor(["z"]), ident), k=2).hypotheses
    assert [(h.output, round(h.cost, 3)) for h in out] == [("z", 0.3)]


def test_edit_cascade_all_four_operations():
    edit = edit_cascade_fst(
        substitutions={("a", "b"): 0.1},
        insertions={"z": 0.05},
        deletions={"d": 0.2},
        alphabet=["a", "b", "c"],
    )
    # 恒等：c -> c 零代价。
    r_id = nbest_paths(compose(chain_acceptor(["c"]), edit), k=2)
    assert r_id.hypotheses[0].output == "c"
    # 替换 a -> b。
    r_sub = nbest_paths(compose(chain_acceptor(["a"]), edit), k=20)
    assert any(h.output == "b" and round(h.cost, 3) == 0.1 for h in r_sub.hypotheses)
    # 删除 d（d 不在 alphabet，但删除弧存在）。
    r_del = nbest_paths(compose(chain_acceptor(["d"]), edit), k=20)
    assert any(h.output == "" and round(h.cost, 3) == 0.2 for h in r_del.hypotheses)
    # 插入 z：输入空不允许（链至少一个符号），用 c 配插入。
    r_ins = nbest_paths(compose(chain_acceptor(["c"]), edit), k=50)
    assert any(h.output == "zc" or h.output == "cz" for h in r_ins.hypotheses)


def test_rewrite_rule_with_identity_alphabet():
    rules = rewrite_rule_fst(
        [("y", "i", 0.3)], alphabet=["y", "x"], name="r"
    )
    chained = compose(chain_acceptor(["x", "y"]), rules)
    res = nbest_paths(chained, k=10)
    # x 恒等 + y->i => "xi"；也存在 x 恒等 + y 恒等 => "xy"，后者 0.3 更贵。
    outs = {h.output: round(h.cost, 3) for h in res.hypotheses}
    assert outs.get("xi") == 0.3
    assert outs.get("xy") == 0.0


def test_edit_cascade_epsilon_arc_labels():
    edit = edit_cascade_fst({}, {"z": 0.1}, {"d": 0.2})
    kinds = {(a.ilabel, a.olabel) for a in edit.arcs}
    assert (EPSILON, "z") in kinds  # 插入：输入 epsilon
    assert ("d", EPSILON) in kinds  # 删除：输出 epsilon
