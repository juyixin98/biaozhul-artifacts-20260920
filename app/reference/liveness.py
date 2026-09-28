"""Independent peak-memory / replay-cost simulator.

This re-derives the accounting rules directly from the documented memory
model and the data-only spec.  It shares no code with :mod:`app.core.memory`;
tests enumerate every checkpoint subset and require the two to agree.

Accounting rules (float64 elements)
-----------------------------------
* root values (inputs/parameters) live for the whole run;
* a forward output freed right after its last forward consumer unless it is
  a retained checkpoint;
* backward walks nodes in reverse topological order.  Before node ``w``'s
  VJP, every non-root input activation that VJP reads (its *seeds*) is made
  available: missing seeds trigger a topological replay of the ancestor
  sub-DAG up to retained/root boundaries;
* a replayed intermediate is freed the instant its last reader *within the
  replay wave* consumed it; a value still read by a later wave is held under
  a reference count and produced exactly once (shared subgraph memoisation);
* a rematerialised dropout's mask is held until the dropout's own VJP; a
  retained dropout redraws its mask transiently at that VJP;
* op scratch: linear/dropout output numel, reduce_sum input numel;
* VJP workspace = op scratch + one contribution buffer per input;
* data-input gradients are reported but not retained; parameter/internal
  gradient buffers persist;
* snapshot RNG strategy pins 624 words when the graph has dropout.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Mapping, Sequence, Set, Tuple

import numpy as np

RNG_STATE_ELEMENTS = 624
ROOTS = ("input", "parameter")
NEEDS_INPUTS = {
    "linear": (0, 1),
    "mul": (0, 1),
    "relu": (0,),
}
SCRATCH_OPS = {"linear", "dropout"}


class LivenessSim:
    def __init__(self, spec: Mapping[str, Any]) -> None:
        self.nodes: Dict[str, dict] = {n["id"]: n for n in spec["nodes"]}
        self.order = self._topo()
        self.target = spec["target"]
        self.shapes = self._shapes()
        self.size = {k: int(np.prod(v)) for k, v in self.shapes.items()}
        self.users = self._users()
        self.rev = [
            nid for nid in reversed(self.order)
            if self.nodes[nid]["op"] not in ROOTS
        ]

    # -- spec parsing -------------------------------------------------------

    def _topo(self) -> List[str]:
        done: Set[str] = set()
        order: List[str] = []
        remaining = dict(self.nodes)
        while remaining:
            ready = sorted(
                nid for nid, n in remaining.items()
                if all(r in done for r in n["inputs"])
            )
            if not ready:
                raise ValueError("cycle")
            for nid in ready:
                order.append(nid)
                done.add(nid)
                remaining.pop(nid)
        return order

    def _shapes(self) -> Dict[str, Tuple[int, ...]]:
        shapes: Dict[str, Tuple[int, ...]] = {}
        for nid in self.order:
            n = self.nodes[nid]
            op = n["op"]
            if op in ROOTS:
                shapes[nid] = tuple(n["params"]["shape"])
            elif op == "linear":
                shapes[nid] = (shapes[n["inputs"][0]][0],
                               shapes[n["inputs"][1]][1])
            elif op == "reduce_sum":
                shapes[nid] = (1,)
            else:
                shapes[nid] = shapes[n["inputs"][0]]
        return shapes

    def _users(self) -> Dict[str, int]:
        users = {nid: 0 for nid in self.nodes}
        for n in self.nodes.values():
            for r in n["inputs"]:
                users[r] += 1
        return users

    # -- costs ---------------------------------------------------------------

    def fwd_cost(self, nid: str) -> int:
        op = self.nodes[nid]["op"]
        if op == "linear":
            b, m = self.shapes[self.nodes[nid]["inputs"][0]]
            ncol = self.shapes[self.nodes[nid]["inputs"][1]][1]
            return 2 * b * m * ncol
        if op == "dropout":
            return 2 * self.size[nid]
        if op in ("add", "mul", "relu", "external", "reduce_sum"):
            return self.size[self.nodes[nid]["inputs"][0]]
        return 0

    def bwd_cost(self, nid: str) -> int:
        op = self.nodes[nid]["op"]
        if op == "linear":
            ins = [self.shapes[r] for r in self.nodes[nid]["inputs"]]
            b, m = ins[0]
            ncol = ins[1][1]
            return 2 * b * m * ncol + (b * ncol if len(ins) == 3 else 0)
        if op in ("add", "mul", "relu", "dropout", "external"):
            return self.size[nid]
        if op == "reduce_sum":
            return self.size[self.nodes[nid]["inputs"][0]]
        return 0

    def scratch(self, nid: str) -> int:
        op = self.nodes[nid]["op"]
        if op in SCRATCH_OPS:
            return self.size[nid]
        if op == "reduce_sum":
            return self.size[self.nodes[nid]["inputs"][0]]
        return 0

    # -- schedule -------------------------------------------------------------

    def _seeds(self, w: str) -> Tuple[str, ...]:
        out: List[str] = []
        for p in NEEDS_INPUTS.get(self.nodes[w]["op"], ()):
            u = self.nodes[w]["inputs"][p]
            if self.nodes[u]["op"] not in ROOTS:
                out.append(u)
        return tuple(out)

    def _closure(self, retained: FrozenSet[str], seeds: Sequence[str]
                 ) -> FrozenSet[str]:
        seen: Set[str] = set()
        stack = [s for s in seeds if s not in retained]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            for u in self.nodes[x]["inputs"]:
                if u in retained or self.nodes[u]["op"] in ROOTS:
                    continue
                stack.append(u)
        return frozenset(seen)

    def _schedule(self, retained_list: Sequence[str]):
        retained = set(retained_list)
        retained.add(self.target)
        R = frozenset(retained)
        rank = {nid: i for i, nid in enumerate(self.order)}
        closures = [
            self._closure(R, self._seeds(w)) for w in self.rev
        ]
        first_step: Dict[str, int] = {}
        for s, closure in enumerate(closures):
            for v in closure:
                first_step.setdefault(v, s)
        produced: Dict[int, List[str]] = {}
        for v, s in first_step.items():
            produced.setdefault(s, []).append(v)
        reads = {nid: 0 for nid in self.rev}
        steps = []
        for s, w in enumerate(self.rev):
            wave = sorted(produced.get(s, ()), key=lambda x: rank[x])
            for v in wave:
                for u in self.nodes[v]["inputs"]:
                    if self.nodes[u]["op"] not in ROOTS:
                        reads[u] += 1
            seeds = self._seeds(w)
            for u in seeds:
                reads[u] += 1
            transient_masks = tuple(
                d for d in ((w,) if self.nodes[w]["op"] == "dropout" else ())
                if d not in first_step
            )
            steps.append((w, tuple(wave), transient_masks, tuple(seeds)))
        return R, steps, set(first_step), reads

    # -- simulation -----------------------------------------------------------

    def simulate(self, retained_list: Sequence[str],
                 rng_strategy: str = "counter") -> Dict[str, Any]:
        R, steps, remat, reads = self._schedule(retained_list)
        live: Dict[str, int] = {}
        peak = 0

        def tick(scratch: int = 0) -> None:
            nonlocal peak
            peak = max(peak, sum(live.values()) + scratch)

        for nid in self.order:
            if self.nodes[nid]["op"] in ROOTS:
                live[f"root:{nid}"] = self.size[nid]
        has_dropout = any(n["op"] == "dropout" for n in self.nodes.values())
        if has_dropout and rng_strategy == "snapshot":
            live["rng:snapshot"] = RNG_STATE_ELEMENTS
        tick()

        fwd_flops = 0
        fwd_refs = dict(self.users)
        for nid in self.order:
            n = self.nodes[nid]
            if n["op"] in ROOTS:
                continue
            fwd_flops += self.fwd_cost(nid)
            tick(self.scratch(nid))
            live[f"act:{nid}"] = self.size[nid]
            tick()
            for u in n["inputs"]:
                if self.nodes[u]["op"] in ROOTS:
                    continue
                fwd_refs[u] -= 1
                if fwd_refs[u] == 0 and u not in R:
                    live.pop(f"act:{u}", None)

        remaining = dict(reads)
        recompute_flops = 0
        bwd_flops = 0
        recomputed: List[str] = []
        mask_only: List[str] = []

        live[f"grad:{self.target}"] = self.size[self.target]
        if remaining.get(self.target, 0) == 0:
            live.pop(f"act:{self.target}", None)
        tick()

        for w, wave, transient_masks, seeds in steps:
            for v in wave:
                recompute_flops += self.fwd_cost(v)
                recomputed.append(v)
                tick(self.scratch(v))
                live[f"act:{v}"] = self.size[v]
                if self.nodes[v]["op"] == "dropout":
                    live[f"mask:{v}"] = self.size[v]
                tick()
                for u in self.nodes[v]["inputs"]:
                    if self.nodes[u]["op"] in ROOTS:
                        continue
                    remaining[u] -= 1
                    if remaining[u] == 0:
                        live.pop(f"act:{u}", None)
                tick()
            for d in transient_masks:
                mask_only.append(d)
                recompute_flops += self.fwd_cost(d)
                tick(self.scratch(d))
            bwd_flops += self.bwd_cost(w)
            vjp_scratch = self.scratch(w) + sum(
                self.size[u] for u in self.nodes[w]["inputs"]
            )
            tick(vjp_scratch)
            for u in self.nodes[w]["inputs"]:
                if self.nodes[u]["op"] == "parameter":
                    live.setdefault(f"grad-param:{u}", self.size[u])
                elif self.nodes[u]["op"] == "input":
                    pass
                else:
                    live.setdefault(f"grad:{u}", self.size[u])
            live.pop(f"grad:{w}", None)
            for u in seeds:
                remaining[u] -= 1
                if remaining[u] == 0:
                    live.pop(f"act:{u}", None)
            live.pop(f"mask:{w}", None)
            tick()

        return {
            "peak_memory": peak,
            "forward_flops": fwd_flops,
            "recompute_flops": recompute_flops,
            "backward_flops": bwd_flops,
            "recomputed_nodes": tuple(sorted(recomputed + mask_only)),
        }
