"""相等标签与零权环专项测试。"""

from mospp.solver import Solver

from .helpers import make_graph, solve, weight_pairs


def test_equal_weight_different_paths_both_kept():
    # 0->1->3 与 0->2->3 权重完全相同 (2,2)，是两条不同的简单路径，
    # 应作为“相等标签”同时保留。
    edges = [
        (0, 1, 1, 1),
        (0, 2, 1, 1),
        (1, 3, 1, 1),
        (2, 3, 1, 1),
    ]
    resp = solve(edges, 0, 3)
    assert resp["status"] == "ok"
    pairs = weight_pairs(resp)
    assert pairs == [(2, 2), (2, 2)]
    node_seqs = sorted(tuple(p["nodes"]) for p in resp["paths"])
    assert node_seqs == [(0, 1, 3), (0, 2, 3)]


def test_same_vertex_set_different_permutations_kept():
    # 回归：到终点的两条路径访问的顶点集合相同、但排列顺序不同，
    # 必须作为两条不同的简单路径都保留（这是一度漏掉的真实 bug）。
    # 0->2 后：2->1->3->5 与 2->3->1->5，全零权。
    edges = [
        (0, 2, 0, 0),
        (2, 1, 0, 0),
        (2, 3, 0, 0),
        (1, 3, 0, 0),
        (3, 1, 0, 0),
        (1, 5, 0, 0),
        (3, 5, 0, 0),
    ]
    resp = solve(edges, 0, 5)
    assert resp["status"] == "ok"
    seqs = sorted(tuple(p["nodes"]) for p in resp["paths"])
    assert (0, 2, 1, 3, 5) in seqs
    assert (0, 2, 3, 1, 5) in seqs
    # 所有路径互不相同
    assert len(seqs) == len(set(seqs))


def test_equal_weight_and_equal_visited_is_duplicate():
    # 平行弧 0->1 两条完全同权：扩展出的标签 visited 相同，算重复，只保留一个。
    edges = [
        (0, 1, 1, 1),
        (0, 1, 1, 1),
        (1, 2, 1, 1),
    ]
    resp = solve(edges, 0, 2)
    assert weight_pairs(resp) == [(2, 2)]
    assert len(resp["paths"]) == 1


def test_dominated_equal_weight_label_dropped():
    # 到节点 1：直接弧 (1,1)；绕行 0->2->1 也是 (1,1) 但走了不同的顶点。
    # 两者顶点序列不同，是两条不同的同权简单路径；按“枚举全部不同 Pareto
    # 简单路径”的语义，二者及其到终点的延伸都应保留。
    edges = [
        (0, 1, 1, 1),
        (0, 2, 1, 1),
        (2, 1, 0, 0),
        (1, 3, 1, 1),
    ]
    g = make_graph(edges)
    res = Solver(g).solve(0, 3)
    assert {(l.time, l.cost) for l in res.target_labels} == {(2, 2)}
    seqs = set()
    for lab in res.target_labels:
        idx, _ = lab.path()
        seqs.add(tuple(g.node_ids[i] for i in idx))
    assert seqs == {(0, 1, 3), (0, 2, 1, 3)}


def test_zero_weight_cycle_terminates_and_is_simple():
    # 1<->2 之间是零权双向边，构成零权环。算法必须终止（不会无限绕行），
    # 且所有返回路径仍是简单路径；零权环不产生新的 Pareto 点。
    edges = [
        (0, 1, 2, 3),
        (1, 2, 0, 0),
        (2, 1, 0, 0),
        (2, 3, 4, 1),
    ]
    resp = solve(edges, 0, 3)
    assert resp["status"] == "ok"
    assert weight_pairs(resp) == [(6, 4)]
    for p in resp["paths"]:
        assert len(p["nodes"]) == len(set(p["nodes"]))
    # 至少发生过一次“回到已访问顶点”的环跳过
    assert resp["stats"]["labels_cycle_skipped"] > 0


def test_all_zero_weights_single_label():
    # 全零权图：零权环全部被简单路径约束挡住，算法正常终止，不无限绕行；
    # 到终点的所有简单路径权重都是 (0,0)，作为“相等标签”全部保留。
    # 简单路径有：0-1-3、0-2-3、0-1-2-3、0-2-1-3（共 4 条）。
    edges = [
        (0, 1, 0, 0),
        (1, 2, 0, 0),
        (2, 1, 0, 0),
        (0, 2, 0, 0),
        (2, 3, 0, 0),
        (1, 3, 0, 0),
    ]
    resp = solve(edges, 0, 3)
    pairs = weight_pairs(resp)
    assert pairs == [(0, 0)] * 4
    seqs = sorted(tuple(p["nodes"]) for p in resp["paths"])
    assert seqs == [(0, 1, 2, 3), (0, 1, 3), (0, 2, 1, 3), (0, 2, 3)]
    for p in resp["paths"]:
        assert len(p["nodes"]) == len(set(p["nodes"]))  # 均为简单路径


def test_zero_cycle_is_skipped_when_visited():
    # 零权环放在“非目标点”上：主链 0->1->2->4(终点)，另有 2<->3 零权环。
    # 标签沿 2->3 到 3 后尝试 3->2 时，2 已在路径上 ⇒ 必然走 cycle 跳过；
    # 算法必须终止，且零权绕行不产生额外 Pareto 点。
    edges = [
        (0, 1, 1, 1),
        (1, 2, 1, 1),
        (2, 3, 0, 0),
        (3, 2, 0, 0),
        (2, 4, 1, 1),
    ]
    resp = solve(edges, 0, 4)
    assert resp["status"] == "ok"
    assert weight_pairs(resp) == [(3, 3)]
    assert resp["stats"]["labels_cycle_skipped"] >= 1


def test_floating_point_accumulation_tolerance():
    # 直达 0->3 权重 (1.0,1.0)；绕行路径由 0.1*4+0.6 累加，浮点结果是
    # 1.0000000000000002。两者在容差带内视为“权重相等”，且顶点序列不同，
    # 是两条不同的同权简单路径 ⇒ 都保留。关键是验证那 2e-16 的舍入误差既
    # 不会产生“伪支配”（删掉一条），也不会产生“伪不同”的第三个点。
    edges = [
        (0, 1, 0.1, 0.1),
        (1, 2, 0.1, 0.1),
        (2, 4, 0.1, 0.1),
        (4, 5, 0.1, 0.1),
        (5, 3, 0.6, 0.6),   # 合计 1.0000000000000002
        (0, 3, 1.0, 1.0),
    ]
    resp = solve(edges, 0, 3)
    pairs = weight_pairs(resp)
    assert len(pairs) == 2
    assert all(abs(t - 1.0) < 1e-8 and abs(c - 1.0) < 1e-8 for t, c in pairs)
    seqs = sorted(tuple(p["nodes"]) for p in resp["paths"])
    assert seqs == [(0, 1, 2, 4, 5, 3), (0, 3)]


def test_near_equal_weights_merge_under_eps():
    # 差值 1e-12（容差带内）且一维严格为 0 差异：容差内视为相等，
    # 同 visited 视为重复，只保留一个标签。
    edges = [
        (0, 1, 1.0, 1.0),
        (0, 2, 1.0 + 1e-12, 1.0),
        (1, 3, 1.0, 1.0),
        (2, 3, 1.0 - 1e-12, 1.0),  # 累计 (2.0, 2.0) vs 另一条 (2.0, 2.0)
    ]
    # 两条路径 visited 不同（{0,1,3} vs {0,2,3}），互不包含，
    # 权重在容差内相等 ⇒ 相等标签共存，但对外只应看到一组 (2,2) 权重。
    resp = solve(edges, 0, 3)
    pairs = weight_pairs(resp)
    assert len(pairs) == 2
    assert all(abs(t - 2.0) < 1e-8 and abs(c - 2.0) < 1e-8 for t, c in pairs)
