"""Synthetic local fixtures: graph builders + deterministic feeds.

Every graph comes with an ``independent_reference`` function written as plain
NumPy expressions. Reference answers are NOT computed by the planner/executor,
so tests compare the system under test against an independent oracle.

Graph catalogue
---------------
diamond             x -> relu -> two branches (add/mul paths) -> join add.
long_lived_output   a small early tensor is a graph output; later waves keep
                    allocating, proving its buffer stays pinned and is never
                    reused.
shape_mutate        dynamic dimension (1..N); a run inside bound succeeds, a
                    run past the bound triggers replanning_required, and the
                    replanned run succeeds again.
parallel_branches   one wave fans out into independent branches that all live
                    simultaneously and join later; parallel execution must not
                    share buffers.
alias_chain         an in-place-style alias forces two tensors onto one buffer.
workspace_matmul    matmul/softmax declare real workspace, counted in peak.
"""
from __future__ import annotations

import numpy as np

from .graph import Graph, Node
from .tensor import TensorSpec

A = 64  # default alignment used across fixtures


# --------------------------------------------------------------------------- #
# Diamond
# --------------------------------------------------------------------------- #

def build_diamond(n: int = 8) -> Graph:
    x = TensorSpec("x", (n,), "float32")
    nodes = (
        Node("r", "relu", ("x",), (TensorSpec("r", (n,), "float32"),)),
        Node("b1", "add", ("r", "x"), (TensorSpec("b1", (n,), "float32"),)),
        Node("b2", "mul", ("r", "x"), (TensorSpec("b2", (n,), "float32"),)),
        Node("join", "add", ("b1", "b2"), (TensorSpec("y", (n,), "float32"),)),
    )
    return Graph("diamond", (x,), nodes, ("y",))


def diamond_feeds(n: int = 8, seed: int = 0) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(1000 + seed)
    return {"x": rng.normal(size=n).astype(np.float32)}


def diamond_reference(x: np.ndarray) -> np.ndarray:
    r = np.maximum(x, 0.0)
    b1 = r + x
    b2 = r * x
    return (b1 + b2).astype(np.float32)


# --------------------------------------------------------------------------- #
# Long-lived output
# --------------------------------------------------------------------------- #

def build_long_lived(n: int = 16, tail: int = 4) -> Graph:
    x = TensorSpec("x", (n,), "float32")
    specs = [TensorSpec("early", (n,), "float32")]
    nodes = [Node("early", "relu", ("x",), (specs[0],))]
    prev = "early"
    for i in range(tail):
        out = f"t{i}"
        nodes.append(Node(out, "add", (prev, "x"), (TensorSpec(out, (n,), "float32"),)))
        prev = out
    nodes.append(Node("final", "mul", (prev, "x"), (TensorSpec("final", (n,), "float32"),)))
    return Graph("long_lived", (x,), tuple(nodes), ("early", "final"))


def long_lived_feeds(n: int = 16, seed: int = 1) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(2000 + seed)
    return {"x": rng.normal(size=n).astype(np.float32) + 0.5}


def long_lived_reference(x: np.ndarray, tail: int = 4):
    early = np.maximum(x, 0.0)
    prev = early
    for _ in range(tail):
        prev = prev + x
    final = prev * x
    return early.astype(np.float32), final.astype(np.float32)


# --------------------------------------------------------------------------- #
# Dynamic shape mutation
# --------------------------------------------------------------------------- #

def build_shape_mutate(capacity: int = 64, bound_n: int = 32) -> Graph:
    # The data source carries ``capacity`` elements; the dynamic output is only
    # declared up to ``bound_n``. A run with bound_n < n <= capacity therefore
    # fits the source but exceeds the planned output capacity and forces a
    # replan; n > capacity cannot be served even by replanning.
    data = TensorSpec("data", (capacity,), "float32")
    length = TensorSpec("length", (), "int64")
    dyn = TensorSpec("dyn", ((1, bound_n),), "float32")
    nodes = (
        Node("tile", "dynamic_tile", ("data", "length"), (dyn,)),
        Node("act", "relu", ("dyn",), (TensorSpec("act", ((1, bound_n),), "float32"),)),
    )
    return Graph("shape_mutate", (data, length), nodes, ("act",))


def shape_mutate_feeds(n: int, capacity: int = 64, seed: int = 2) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(3000 + seed)
    data = np.empty(capacity, dtype=np.float32)
    data[:] = rng.normal(size=capacity)
    data[0] = -1.0  # guarantee a negative so relu changes something
    return {"data": data, "length": np.array(n, dtype=np.int64)}


def shape_mutate_reference(data: np.ndarray, n: int) -> np.ndarray:
    return np.maximum(data[:n], 0.0).astype(np.float32)


# --------------------------------------------------------------------------- #
# Parallel branches
# --------------------------------------------------------------------------- #

def build_parallel_branches(n: int = 12, branches: int = 3) -> Graph:
    x = TensorSpec("x", (n,), "float32")
    nodes: list[Node] = []
    names = []
    for b in range(branches):
        out = f"p{b}"
        # relu on the first branch, identity chains on others so all branch
        # nodes are independent and scheduled in the same wave.
        op = "relu" if b == 0 else "identity"
        nodes.append(Node(out, op, ("x",), (TensorSpec(out, (n,), "float32"),)))
        names.append(out)
    acc = names[0]
    for b in range(1, branches):
        merged = f"m{b}"
        nodes.append(Node(merged, "add", (acc, names[b]), (TensorSpec(merged, (n,), "float32"),)))
        acc = merged
    return Graph("parallel_branches", (x,), tuple(nodes), (acc,))


def parallel_feeds(n: int = 12, seed: int = 3) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(4000 + seed)
    return {"x": (rng.normal(size=n) - 0.5).astype(np.float32)}


def parallel_reference(x: np.ndarray, branches: int = 3) -> np.ndarray:
    vals = [np.maximum(x, 0.0)] + [x for _ in range(branches - 1)]
    acc = vals[0]
    for v in vals[1:]:
        acc = acc + v
    return acc.astype(np.float32)


# --------------------------------------------------------------------------- #
# Alias chain (in-place view forces union)
# --------------------------------------------------------------------------- #

def build_alias(n: int = 8) -> Graph:
    x = TensorSpec("x", (n,), "float32")
    nodes = (
        # out a aliases input x: they must share one allocation for the hull.
        Node("a", "identity", ("x",), (TensorSpec("a", (n,), "float32"),), aliases=(("a", "x"),)),
        Node("b", "relu", ("a",), (TensorSpec("b", (n,), "float32"),)),
        Node("c", "add", ("b", "a"), (TensorSpec("c", (n,), "float32"),)),
    )
    return Graph("alias_chain", (x,), nodes, ("c",))


def alias_feeds(n: int = 8, seed: int = 4) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(5000 + seed)
    return {"x": (rng.normal(size=n) - 0.5).astype(np.float32)}


def alias_reference(x: np.ndarray) -> np.ndarray:
    a = x
    b = np.maximum(a, 0.0)
    return (b + a).astype(np.float32)


# --------------------------------------------------------------------------- #
# Workspace pressure
# --------------------------------------------------------------------------- #

def build_workspace_matmul(m: int = 4, k: int = 5, n: int = 6) -> Graph:
    a = TensorSpec("a", (m, k), "float32")
    b = TensorSpec("b", (k, n), "float32")
    nodes = (
        Node("mm", "matmul", ("a", "b"), (TensorSpec("mm", (m, n), "float32"),)),
        Node("sm", "softmax", ("mm",), (TensorSpec("sm", (m, n), "float32"),)),
    )
    return Graph("workspace_matmul", (a, b), nodes, ("sm",))


def workspace_feeds(m: int = 4, k: int = 5, n: int = 6, seed: int = 5) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(6000 + seed)
    return {
        "a": rng.normal(size=(m, k)).astype(np.float32),
        "b": rng.normal(size=(k, n)).astype(np.float32),
    }


def workspace_reference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    mm = a @ b
    e = np.exp(mm - np.max(mm, axis=-1, keepdims=True))
    return (e / np.sum(e, axis=-1, keepdims=True)).astype(np.float32)
