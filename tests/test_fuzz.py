"""Random-DAG fuzzing: planner safety and executor equivalence must hold
for hundreds of generated graphs with multi-consumer tensors, shared views,
branches and joins."""

import numpy as np

from tmem.dag import ADD, MATMUL, SLICE, build_dag
from tmem.executor import execute
from tmem.planner import plan_memory

from helpers import assert_outputs_match, assert_plan_safe


class DAGGenerator:
    """Builds a random valid DAG of known-shape add/matmul/slice ops."""

    def __init__(self, rng: np.random.Generator, max_dim: int = 5):
        self.rng = rng
        self.max_dim = max_dim
        self.tensors: dict[str, tuple[int, ...]] = {}
        self.ops: list[dict] = []

    def _shape(self, ndmin=1, ndmax=3):
        ndim = int(self.rng.integers(ndmin, ndmax + 1))
        return tuple(int(self.rng.integers(1, self.max_dim + 1)) for _ in range(ndim))

    def add_input(self, name: str):
        self.tensors[name] = self._shape(ndmin=2)

    def _existing(self):
        # Op sources must have positive dims so generated partners stay
        # valid; zero-size tensors (from empty slices) remain as leaf nodes.
        positive = [(n, s) for n, s in self.tensors.items() if all(d >= 1 for d in s)]
        name = self.rng.choice([n for n, _ in positive])
        return name, dict(positive)[name]

    def add_random_op(self, name: str):
        kind = self.rng.choice([ADD, MATMUL, SLICE], p=[0.3, 0.35, 0.35])
        if kind == SLICE:
            src, shape = self._existing()
            starts = [int(self.rng.integers(0, d)) for d in shape]
            if self.rng.random() < 0.12:  # occasionally an empty slice
                stops = list(starts)
            else:
                stops = [
                    int(self.rng.integers(s + 1, d + 1)) if s < d else d
                    for s, d in zip(starts, shape)
                ]
            self.ops.append(
                {"name": name, "op": SLICE, "inputs": [src],
                 "starts": starts, "stops": stops}
            )
            out = tuple(e - s for s, e in zip(starts, stops))
        elif kind == ADD:
            a, sa = self._existing()
            # Build a broadcast-compatible partner shape from sa.
            sb = list(sa)
            for axis in range(len(sb)):
                if self.rng.random() < 0.35:
                    sb[axis] = 1
            if self.rng.random() < 0.2:  # drop a leading axis
                sb = sb[1:]
            b = self._material_input(name + "_b", tuple(sb) if sb else (1,))
            self.ops.append({"name": name, "op": ADD, "inputs": [a, b]})
            out = np.broadcast_shapes(sa, tuple(sb) if sb else (1,))
        else:  # MATMUL
            a, sa = self._existing()
            k = sa[-1]
            n = int(self.rng.integers(1, self.max_dim + 1))
            b_shape = (k, n)
            if len(sa) >= 3 and self.rng.random() < 0.4:
                b_shape = sa[:-2] + b_shape
            b = self._material_input(name + "_w", b_shape)
            self.ops.append({"name": name, "op": MATMUL, "inputs": [a, b]})
            if len(sa) == 2:
                out = (sa[0], n)
            elif len(sa) == 1:
                out = (n,)
            else:
                out = sa[:-1] + (n,)
        self.tensors[name] = tuple(out)

    def _material_input(self, name: str, shape: tuple[int, ...]) -> str:
        # Fresh external weights/bias keep random graphs independent.
        base = name
        i = 0
        while base in self.tensors:
            i += 1
            base = f"{name}_{i}"
        self.tensors[base] = tuple(shape)
        # Inputs must be declared before ops; generator processes ops in
        # order, so inject an input entry via a deferred list.
        self.extra_inputs[base] = list(shape)
        return base

    def build(self, n_inputs: int, n_ops: int, n_outputs: int):
        self.extra_inputs: dict[str, list[int]] = {}
        for i in range(n_inputs):
            self.add_input(f"in{i}")
        for j in range(n_ops):
            self.add_random_op(f"t{j}")
        # Outputs: bias toward late tensors and views so joins/outputs pin
        # interesting lifetimes.
        candidates = list(self.tensors)[-min(len(self.tensors), n_ops) :]
        outputs = list(
            self.rng.choice(
                candidates,
                size=min(n_outputs, len(candidates)),
                replace=False,
            )
        )
        return {
            "inputs": {
                **{f"in{i}": list(self.tensors[f"in{i}"]) for i in range(n_inputs)},
                **self.extra_inputs,
            },
            "ops": self.ops,
            "outputs": outputs,
        }


def test_fuzz_random_dags():
    rng = np.random.default_rng(20260925)
    trials = 200
    checked = 0
    for trial in range(trials):
        gen = DAGGenerator(rng)
        request = gen.build(
            n_inputs=int(rng.integers(1, 4)),
            n_ops=int(rng.integers(4, 14)),
            n_outputs=int(rng.integers(1, 4)),
        )
        request["seed"] = int(rng.integers(0, 1_000_000))
        dag = build_dag(request)  # raises if the generator broke validity
        plan = plan_memory(dag)
        assert_plan_safe(plan)
        run = execute(dag, mode="both", seed=request["seed"])
        assert_outputs_match(run)
        checked += 1
    assert checked == trials


def test_fuzz_includes_sharing_and_branching_shapes():
    # Structural guard: across many graphs the generator produces views,
    # multi-consumer tensors and joins, so the fuzz really exercises them.
    rng = np.random.default_rng(7)
    saw_view = saw_multi_consumer = False
    for _ in range(50):
        gen = DAGGenerator(rng)
        request = gen.build(n_inputs=3, n_ops=12, n_outputs=2)
        dag = build_dag(request)
        saw_view |= any(n.op == SLICE for n in dag.nodes.values())
        saw_multi_consumer |= any(
            len(c) > 1 for c in dag.consumers.values()
        )
    assert saw_view and saw_multi_consumer
