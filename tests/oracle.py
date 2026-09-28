"""独立穷举参考机（测试专用）。

该解释器**不导入**组合或最短路径核心（``compose`` / ``nbest_paths``），
直接对一串 FST 逐级做带插入界的 DFS 字符串解释，min-plus 合并，
从而为短输入提供一份与被测核心相互独立的穷举答案，用于交叉核验：

* 每一级 ``apply_cascade``：从初态出发递归，消耗输入的弧（ilabel 为
  真实符号）前进一步；输入 epsilon 弧（插入）不消耗输入，用
  ``max_insertions`` 计数封顶（插入有正代价，短输入下取足够大的界不影响
  最优 k 条，测试用两个不同的界断言结果稳定）；
* 输出 epsilon（删除）自然处理为「不追加输出」；
* 三级（edit、rules、lexicon）顺序应用，词典是前向 DAG，无输入 epsilon，
  因此最后一级严格有界。

这份参考与 :func:`wfst.query.run_query` 走完全不同的代码路径，
避免「参考答案由被测核心自己生成」。
"""

from __future__ import annotations

from wfst.core.fst import EPSILON


def apply_cascade(fst, tokens, max_insertions):
    """把单个 FST 应用到符号序列，返回 {输出元组: 最小代价}。"""
    out_index = fst.outgoing
    results: dict[tuple[str, ...], float] = {}

    def dfs(state, pos, emitted, cost, inserts_left):
        if pos == len(tokens) and fst.is_final(state):
            # 终止权重必须计入（词典条目代价放在终态）。
            total = cost + fst.final_weight(state)
            key = tuple(emitted)
            if key not in results or total < results[key]:
                results[key] = total
        for arc in out_index(state):
            if arc.ilabel == EPSILON:
                # 输入 epsilon：插入（不消耗输入）。封顶防止无穷递归。
                if inserts_left <= 0:
                    continue
                dfs(
                    arc.dst,
                    pos,
                    emitted + ([] if arc.olabel == EPSILON else [arc.olabel]),
                    cost + arc.weight,
                    inserts_left - 1,
                )
            else:
                if pos < len(tokens) and tokens[pos] == arc.ilabel:
                    dfs(
                        arc.dst,
                        pos + 1,
                        emitted + ([] if arc.olabel == EPSILON else [arc.olabel]),
                        cost + arc.weight,
                        inserts_left,
                    )

    dfs(fst.start, 0, [], 0.0, max_insertions)
    return results


def _merge_next(prev, fst, max_insertions):
    """对前级的每个输出应用下一级，累计代价取 min。"""
    merged: dict[tuple[str, ...], float] = {}
    for mid_tokens, base_cost in prev.items():
        for out_tokens, add_cost in apply_cascade(
            fst, list(mid_tokens), max_insertions
        ).items():
            total = base_cost + add_cost
            if out_tokens not in merged or total < merged[out_tokens]:
                merged[out_tokens] = total
    return merged


def oracle_topk(model, tokens, k, max_insertions=4):
    """三级顺序穷举，返回与 run_query 同口径的 [(输出拼接串, 代价)]。"""
    first = apply_cascade(model.edit, tokens, max_insertions)
    second = _merge_next(first, model.rules, max_insertions)
    third = _merge_next(second, model.lexicon, max_insertions)
    joiner = "" if model.token_level == "char" else "\x00"
    ranked = sorted(
        ((joiner.join(out), cost) for out, cost in third.items()),
        key=lambda x: (x[1], x[0]),
    )
    return ranked[:k]
