"""Synthetic local fixtures and an *independent* NumPy reference oracle.

The reference computations here are written as direct NumPy expressions over
freshly generated arrays -- they never call the planner, executor or graph
machinery. Tests compare executed output values against these independent
references element by element.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .graph import Graph, GraphBuilder
from .tensor import TensorMeta


# --------------------------------------------------------------------------- #
# Deterministic data
# --------------------------------------------------------------------------- #


def seeded_input(shape: tuple[int, ...], dtype: str, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(shape).astype(dtype)


# --------------------------------------------------------------------------- #
# Graph factories
# --------------------------------------------------------------------------- #


def build_diamond_graph() -> Graph:
    """x -> relu -> add/mul branches -> join add.

    Layout (waves)::

        wave0: n_relu
        wave1: n_add_branch, n_mul_branch   (parallel branches)
        wave2: n_join
    """
    b = GraphBuilder()
    b.feed("x", "float32", (4, 4))
    b.node("n_relu", "relu", ["x"], ["t"])
    b.node("n_add_branch", "add", ["t", "x"], ["a"])
    b.node("n_mul_branch", "mul", ["t", "t"], ["m"])
    b.node("n_join", "add", ["a", "m"], ["y"])
    b.graph_outputs(["y"])
    return b.build()


def build_long_lived_graph() -> Graph:
    """Intermediate ``early`` is retained as a graph output while later nodes
    keep producing -- its storage must stay live and never be reused."""
    b = GraphBuilder()
    b.feed("x", "float32", (8,))
    b.node("n1", "relu", ["x"], ["early"])
    b.node("n2", "relu", ["early"], ["late1"])
    b.node("n3", "add", ["late1", "late1"], ["late2"])
    b.node("n4", "mul", ["late2", "late2"], ["final"])
    b.graph_outputs(["early", "final"])
    return b.build()


def build_dynamic_matmul_graph() -> Graph:
    """Static declaration uses (k=4, n=2); runs may feed other batch sizes."""
    b = GraphBuilder()
    b.feed("a", "float32", (4, 4))
    b.feed("b", "float32", (4, 2))
    b.node("n_mm", "matmul", ["a", "b"], ["c"])
    b.node("n_relu", "relu", ["c"], ["y"])
    b.graph_outputs(["y"])
    return b.build()


def build_concurrent_branches_graph(branches: int = 3) -> Graph:
    """``branches`` independent matmul arms share the same wave; their outputs
    are all retained, exercising the parallel-branch no-sharing rule."""
    b = GraphBuilder()
    b.feed("x", "float32", (4, 4))
    out_names: list[str] = []
    for i in range(branches):
        w_name = f"w{i}"
        o_name = f"o{i}"
        b.feed(w_name, "float32", (4, 4))
        b.node(f"n_branch_{i}", "matmul", ["x", w_name], [o_name])
        out_names.append(o_name)
    b.graph_outputs(out_names)
    return b.build()


def build_workspace_graph() -> Graph:
    """Two matmuls at different waves can share workspace/pool storage;
    scratch bytes are nonzero so peak accounting must include them."""
    b = GraphBuilder()
    b.feed("a", "float32", (4, 8))
    b.feed("b", "float32", (8, 2))
    b.feed("c", "float32", (2, 3))
    b.node("n_mm1", "matmul", ["a", "b"], ["t"])
    b.node("n_mm2", "matmul", ["t", "c"], ["y"])
    b.graph_outputs(["y"])
    return b.build()


def build_alias_graph() -> Graph:
    """reshape/transpose views must alias source storage, not take new slots."""
    b = GraphBuilder()
    b.feed("x", "float32", (2, 6))
    b.node("n_relu", "relu", ["x"], ["r"])
    b.node("n_reshape", "reshape", ["r"], ["v"], {"shape": [3, 4]})
    b.node("n_transpose", "transpose", ["v"], ["z"])
    b.graph_outputs(["r", "z"])
    return b.build()


def build_linear_grad_graph() -> Graph:
    """Forward: y = W @ x (W: rxc, x: cxb -> rxb).
    Synthetic grad: grad_W = g @ x.T  (g: rxb, x.T: bxc -> rxc).
    Feeds W (parameter), x (input), g (upstream gradient). Output y, grad_W.
    """
    b = GraphBuilder()
    b.feed("W", "float32", (3, 4))
    b.feed("x", "float32", (4, 2))
    b.feed("g", "float32", (3, 2))
    b.node("n_fwd", "matmul", ["W", "x"], ["y"])
    b.node("n_xt", "transpose", ["x"], ["xt"])
    b.node("n_grad", "matmul", ["g", "xt"], ["grad_W"])
    b.graph_outputs(["y", "grad_W"])
    return b.build()


GRAPH_FACTORIES = {
    "diamond": build_diamond_graph,
    "long_lived": build_long_lived_graph,
    "dynamic_matmul": build_dynamic_matmul_graph,
    "concurrent_branches": build_concurrent_branches_graph,
    "workspace": build_workspace_graph,
    "alias": build_alias_graph,
    "linear_grad": build_linear_grad_graph,
}


def graph_fingerprint(graph: Graph) -> tuple[tuple[str, str], ...]:
    """Structural identity used to match a session graph to a fixture case."""
    return tuple((nid, graph.nodes[nid].op) for nid in graph.order)


# --------------------------------------------------------------------------- #
# Feed sets
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Case:
    name: str
    graph_factory: str
    feeds: dict[str, np.ndarray]
    # Parameters the graph factory must receive, so the graph always matches
    # the feed arrays (e.g. the concurrent-branch count).
    params: dict = field(default_factory=dict)

    def graph(self) -> Graph:
        if self.graph_factory == "concurrent_branches":
            return build_concurrent_branches_graph(self.params["branches"])
        return GRAPH_FACTORIES[self.graph_factory]()


def diamond_case(seed: int = 11) -> Case:
    x = seeded_input((4, 4), "float32", seed)
    return Case("diamond", "diamond", {"x": x})


def long_lived_case(seed: int = 23) -> Case:
    return Case("long_lived", "long_lived", {"x": seeded_input((8,), "float32", seed)})


def dynamic_case(
    m: int = 8, k: int = 4, n: int = 2, seed: int = 37
) -> Case:
    return Case(
        f"dynamic_{m}x{k}x{n}", "dynamic_matmul",
        {
            "a": seeded_input((m, k), "float32", seed),
            "b": seeded_input((k, n), "float32", seed + 1),
        },
    )


def concurrent_case(branches: int = 3, seed: int = 51) -> Case:
    feeds: dict[str, np.ndarray] = {"x": seeded_input((4, 4), "float32", seed)}
    for i in range(branches):
        feeds[f"w{i}"] = seeded_input((4, 4), "float32", seed + 100 + i)
    return Case(
        "concurrent", "concurrent_branches", feeds,
        params={"branches": branches},
    )


def workspace_case(seed: int = 61) -> Case:
    return Case(
        "workspace", "workspace",
        {
            "a": seeded_input((4, 8), "float32", seed),
            "b": seeded_input((8, 2), "float32", seed + 1),
            "c": seeded_input((2, 3), "float32", seed + 2),
        },
    )


def alias_case(seed: int = 71) -> Case:
    return Case("alias", "alias", {"x": seeded_input((2, 6), "float32", seed)})


def linear_grad_case(r: int = 3, c: int = 4, b: int = 2, seed: int = 83) -> Case:
    return Case(
        "linear_grad", "linear_grad",
        {
            "W": seeded_input((r, c), "float32", seed),
            "x": seeded_input((c, b), "float32", seed + 1),
            "g": seeded_input((r, b), "float32", seed + 2),
        },
    )


# --------------------------------------------------------------------------- #
# Independent reference oracle (plain NumPy; no code under test involved)
# --------------------------------------------------------------------------- #


def reference_outputs(case: Case) -> dict[str, np.ndarray]:
    if case.name == "diamond" or case.graph_factory == "diamond":
        x = case.feeds["x"]
        t = np.maximum(x, 0.0)
        a = t + x
        m = t * t
        y = a + m
        return {"y": y.astype(np.float32)}
    if case.graph_factory == "long_lived":
        x = case.feeds["x"]
        early = np.maximum(x, 0.0)
        late1 = np.maximum(early, 0.0)
        late2 = late1 + late1
        final = late2 * late2
        return {"early": early, "final": final}
    if case.graph_factory == "dynamic_matmul":
        a, b = case.feeds["a"], case.feeds["b"]
        return {"y": np.maximum(a @ b, 0.0)}
    if case.graph_factory == "concurrent_branches":
        x = case.feeds["x"]
        return {
            f"o{i}": x @ case.feeds[f"w{i}"]
            for i in range(len(case.feeds) - 1)
        }
    if case.graph_factory == "workspace":
        a, b, c = case.feeds["a"], case.feeds["b"], case.feeds["c"]
        return {"y": (a @ b) @ c}
    if case.graph_factory == "alias":
        x = case.feeds["x"]
        r = np.maximum(x, 0.0)
        z = r.reshape(3, 4).T
        return {"r": r, "z": z}
    if case.graph_factory == "linear_grad":
        W, x, g = case.feeds["W"], case.feeds["x"], case.feeds["g"]
        return {"y": W @ x, "grad_W": g @ x.T}
    raise KeyError(f"no independent reference for case {case.name!r}")


def no_reuse_peak_bytes(graph: Graph, alignment: int = 64) -> int:
    """Naive baseline every planner result is compared against: one aligned
    allocation per value tensor plus per-node workspace, summed globally."""
    from .liveness import analyze_liveness

    live = analyze_liveness(graph, alignment)
    return sum(r.bytes_required for r in live.records)


def static_meta_dict(graph: Graph) -> dict[str, TensorMeta]:
    metas = dict(graph.feeds)
    for nid in graph.order:
        node = graph.nodes[nid]
        for out, meta in zip(node.outputs, node.out_meta):
            metas[out] = meta
    return metas
