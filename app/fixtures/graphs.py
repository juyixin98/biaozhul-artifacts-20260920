"""Build validated core fixtures from the neutral data-only specs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Tuple

import numpy as np

from ..core.graph import Node, build_graph
from ..core.state import ParameterInit, TrainingState
from ..core.tensor import SUPPORTED_DTYPE
from .specs import ALL_SPECS


@dataclass(frozen=True)
class Fixture:
    name: str
    nodes: Tuple[Node, ...]
    target: str
    parameter_inits: Mapping[str, ParameterInit]
    inputs: Mapping[str, np.ndarray]
    description: str

    def graph(self):
        return build_graph(list(self.nodes), [self.target])

    def training_state(self) -> TrainingState:
        return TrainingState(dict(self.parameter_inits))


def _from_spec(spec: Dict[str, Any]) -> Fixture:
    nodes = tuple(
        Node(
            id=n["id"],
            op=n["op"],
            inputs=tuple(n.get("inputs", ())),
            params=dict(n.get("params", {})),
        )
        for n in spec["nodes"]
    )
    inits = {
        pid: ParameterInit(shape=tuple(meta["shape"]), seed=meta["seed"])
        for pid, meta in spec["parameters"].items()
    }
    inputs = {
        k: np.asarray(v, dtype=SUPPORTED_DTYPE) for k, v in spec["inputs"].items()
    }
    return Fixture(
        name=spec["name"],
        nodes=nodes,
        target=spec["target"],
        parameter_inits=inits,
        inputs=inputs,
        description=spec["description"],
    )


LINEAR_CHAIN = lambda: _from_spec(ALL_SPECS["linear_chain"])
BRANCHING = lambda: _from_spec(ALL_SPECS["branching"])
TIGHT_BUDGET = lambda: _from_spec(ALL_SPECS["tight_budget"])


def get_fixture(name: str) -> Fixture:
    if name not in ALL_SPECS:
        raise KeyError(f"unknown fixture {name!r}; choose from {sorted(ALL_SPECS)}")
    return _from_spec(ALL_SPECS[name])


def all_fixtures() -> List[Fixture]:
    return [_from_spec(s) for s in ALL_SPECS.values()]
