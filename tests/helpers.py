"""测试辅助：构图、发请求的小工具。"""

from __future__ import annotations

from mospp.api import solve_request
from mospp.graph import Graph


def make_graph(edges, nodes=None, directed=True):
    """edges: [(u,v,time,cost), ...] -> 已 freeze 的 Graph。

    若未显式给 nodes，按出现在边中的顶点 ID 排序后预注册，保证整数 ID 与
    内部下标一致（0..n-1），测试里才可以直接用整数当下标。
    """
    g = Graph()
    if nodes is None:
        ids = sorted({x for (u, v, _t, _c) in edges for x in (u, v)})
    else:
        ids = nodes
    for nid in ids:
        g.add_node(nid)
    for i, (u, v, t, c) in enumerate(edges):
        g.add_arc(u, v, t, c, key=i)
        if not directed:
            g.add_arc(v, u, t, c, key=i)
    g.freeze()
    return g


def req(edges, source=0, target=3, *, nodes=None, directed=True, **kw):
    r = {
        "edges": [
            {"from": u, "to": v, "time": t, "cost": c}
            for (u, v, t, c) in edges
        ],
        "source": source,
        "target": target,
        "directed": directed,
    }
    if nodes is not None:
        r["nodes"] = nodes
    r.update(kw)
    return r


def solve(edges, source=0, target=3, **kw):
    return solve_request(req(edges, source, target, **kw))


def weight_pairs(resp):
    return sorted((p["time"], p["cost"]) for p in resp["pareto"])
