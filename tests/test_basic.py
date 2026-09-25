"""基础正确性：Pareto 解、路径重建、源=目标、不可达。"""

import math

from mospp.solver import Solver

from .helpers import make_graph, solve, weight_pairs


# 经典双目标菱形图：
#   0 -> 1 (1, 4), 0 -> 2 (2, 1), 1 -> 3 (1, 1), 2 -> 3 (2, 4)
# 直达快速路 0 -> 3 (5, 0)
# 候选路径：0-1-3=(2,5)，0-2-3=(4,5)（被前者支配），直达=(5,0)
EDGES = [
    (0, 1, 1, 4),
    (0, 2, 2, 1),
    (1, 3, 1, 1),
    (2, 3, 2, 4),
    (0, 3, 5, 0),
]
EXPECTED = {(2, 5), (5, 0)}


def test_diamond_pareto():
    resp = solve(EDGES, 0, 3)
    assert resp["status"] == "ok"
    assert set(weight_pairs(resp)) == EXPECTED
    # 两条非支配路径
    assert len(resp["paths"]) == 2


def test_path_reconstruction_weights_and_nodes():
    resp = solve(EDGES, 0, 3)
    for path in resp["paths"]:
        nodes = path["nodes"]
        # 重建路径是合法简单路径
        assert nodes[0] == 0 and nodes[-1] == 3
        assert len(nodes) == len(set(nodes))
        # 边序列首尾相连，且累计权重与标签一致
        assert len(path["edges"]) == len(nodes) - 1
        tt = sum(e["time"] for e in path["edges"])
        tc = sum(e["cost"] for e in path["edges"])
        assert math.isclose(tt, path["time"], abs_tol=1e-9)
        assert math.isclose(tc, path["cost"], abs_tol=1e-9)
        for i, e in enumerate(path["edges"]):
            assert e["from"] == nodes[i]
            assert e["to"] == nodes[i + 1]


def test_solver_labels_match_reconstructed():
    g = make_graph(EDGES)
    res = Solver(g).solve(0, 3)
    assert res.status == "ok"
    got = set()
    for lab in res.target_labels:
        node_idx, arcs = lab.path()
        assert g.node_ids[node_idx[0]] == 0
        assert g.node_ids[node_idx[-1]] == 3
        assert sum(a.time for a in arcs) == lab.time
        assert sum(a.cost for a in arcs) == lab.cost
        got.add((lab.time, lab.cost))
    assert got == EXPECTED


def test_unreachable():
    edges = [(0, 1, 1, 1), (2, 3, 1, 1)]
    resp = solve(edges, 0, 3)
    assert resp["status"] == "unreachable"
    assert resp["paths"] == []
    assert resp["pareto"] == []


def test_source_equals_target():
    resp = solve(EDGES, 0, 0)
    assert resp["status"] == "ok"
    # 简单路径语义下 s->s 只有零权空路径（零权环不允许绕行回源点）
    assert weight_pairs(resp) == [(0, 0)]
    assert resp["paths"][0]["nodes"] == [0]
    assert resp["paths"][0]["edges"] == []


def test_output_is_json_serializable():
    import json

    resp = solve(EDGES, 0, 3)
    s = json.dumps(resp, ensure_ascii=False)
    again = json.loads(s)
    assert again["status"] == "ok"
