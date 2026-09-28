"""交叉核验测试：被测核心 vs 独立穷举 oracle vs 分阶段顺序执行。

短输入上对全部候选做穷举对照：
1. ``run_query``（一次性组合）与独立 DFS 参考机 :mod:`oracle` 结果一致；
2. ``run_query`` 与 ``transduce_stagewise``（分阶段顺序执行）结果一致；
3. oracle 在两个不同插入界下结果稳定（界足够大时不依赖该参数）。

oracle 不导入 compose/nbest，故参考答案不由被测核心自身生成。
"""

from __future__ import annotations

from oracle import oracle_topk

from wfst.query import (
    render_output,
    run_query,
    tokenize_for_model,
    transduce_stagewise,
)

CHAR_INPUTS = ["kat", "kats", "bagz", "bags", "citi", "doog", "stpo", "stop"]
EPS_INPUTS = ["ab", "b", "bc"]
WORD_INPUTS = ["go", "walk", "walk slowly"]


def _composed_signature(model, text, k=12):
    resp = run_query(model, text, k=k, budget=200_000)
    return (
        [
            (render_output(h.output, model.token_level), round(h.cost, 9))
            for h in resp.hypotheses
        ],
        resp,
    )


def _stagewise_signature(model, text, k=12):
    resp = transduce_stagewise(model, text, k=k, budget=200_000, beam_width=400)
    return [
        (render_output(h.output, model.token_level), round(h.cost, 9))
        for h in resp.hypotheses
    ]


def _oracle_signature(model, text, bound, k=12):
    tokens = tokenize_for_model(text, model)
    raw = oracle_topk(model, tokens, k=k, max_insertions=bound)
    return [
        (render_output(out, model.token_level), round(cost, 9))
        for out, cost in raw
    ]


def test_char_fixture_matches_independent_oracle(char_model):
    for text in CHAR_INPUTS:
        core, resp = _composed_signature(char_model, text)
        oracle4 = _oracle_signature(char_model, text, 4)
        oracle8 = _oracle_signature(char_model, text, 8)
        assert core == oracle4, f"{text}: 核心与 oracle(界4) 不一致"
        assert core == oracle8, f"{text}: 核心与 oracle(界8) 不一致"
        assert resp.complete is True


def test_char_fixture_composed_equals_stagewise(char_model):
    for text in CHAR_INPUTS:
        core, _ = _composed_signature(char_model, text)
        staged = _stagewise_signature(char_model, text)
        assert core == staged, f"{text}: 组合 {core} != 顺序 {staged}"


def test_epsilon_fixture_matches_oracle_and_stagewise(eps_model):
    for text in EPS_INPUTS:
        core, resp = _composed_signature(eps_model, text, k=20)
        oracle4 = _oracle_signature(eps_model, text, 4, k=20)
        oracle8 = _oracle_signature(eps_model, text, 8, k=20)
        staged = _stagewise_signature(eps_model, text, k=20)
        assert core == oracle4 == oracle8, f"{text}: {core} vs {oracle4}"
        assert core == staged
        assert resp.complete is True


def test_epsilon_fixture_has_specific_ambiguous_outputs(eps_model):
    # 明确断言具体输出（而非仅"接口可调"）：输入 ab 同时映射到 B/AB/ab。
    core, _ = _composed_signature(eps_model, "ab", k=20)
    outputs = [o for o, _ in core]
    assert outputs == ["B", "AB", "ab"]
    # 代价严格升序。
    costs = [c for _, c in core]
    assert costs == sorted(costs)


def test_word_level_fixture_matches_oracle(word_model):
    for text in WORD_INPUTS:
        core, resp = _composed_signature(word_model, text, k=12)
        staged = _stagewise_signature(word_model, text, k=12)
        tokens = tokenize_for_model(text, word_model)
        oracle = [
            (render_output(o, "word"), round(c, 9))
            for o, c in oracle_topk(word_model, tokens, k=12, max_insertions=4)
        ]
        assert core == oracle, f"词级 {text}: {core} vs {oracle}"
        assert core == staged
        # 词级输出必须以空格连接，不残留内部边界符。
        assert all("\x00" not in o for o, _ in core)
        assert resp.complete is True


def test_word_level_specific_outputs(word_model):
    core, _ = _composed_signature(word_model, "go", k=12)
    outputs = [o for o, _ in core]
    # go 的词典义项 + 规则 go->goes 都会出现，go 自身代价最低。
    assert outputs[0] == "go"
    assert set(outputs) >= {"go", "goes"}
