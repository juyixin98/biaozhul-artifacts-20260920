"""Acceptance tests against an INDEPENDENT reference implementation.

The oracle in this module deliberately shares no code with the planner
under test: it parses the JSON rule language itself, grounds actions with
itertools, encodes states as integer bitmasks and runs exhaustive BFS /
Dijkstra. The service kernel may only agree with it; it cannot be used to
generate the expected answers.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

import pytest

from strips_planner.executor import execute_plan
from strips_planner.grounding import ground
from strips_planner.heuristics import MaxHeuristic
from strips_planner.parser import parse_domain, parse_problem
from strips_planner.search import ASTAR, BFS, UCS, SearchConfig, search
from strips_planner.validation import validate

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# --------------------------------------------------------------------------- #
# Independent oracle (written only against the JSON language description)
# --------------------------------------------------------------------------- #

class IndependentOracle:
    """Exhaustive bitmask planner with its own parser and grounding."""

    _TOKEN = re.compile(r"^~?\s*([A-Za-z_][\w-]*)(?:\(([^)]*)\))?$")

    def __init__(self, request: dict):
        self.objects = list(request["problem"]["objects"])
        self.fact_index: dict[tuple, int] = {}
        self.facts: list[tuple] = []

        actions_json = request["domain"]["actions"]
        self.schema_costs = {a["name"]: a.get("cost", 1) for a in actions_json}
        self.schemas = [
            (a["name"], list(a["parameters"]),
             self._literals(a.get("preconditions", [])),
             self._literals(a.get("add", [])),
             self._literals(a.get("delete", [])),
             a.get("cost", 1))
            for a in actions_json
        ]

        init = self._literals(request["problem"].get("init", []))
        goal = self._literals(request["problem"].get("goal", []))
        self.initial_mask = 0
        for atom, neg in init:
            assert not neg
            self.initial_mask |= self._bit(atom)
        self.goal_pos_mask = 0
        self.goal_neg_mask = 0
        for atom, neg in goal:
            if neg:
                self.goal_neg_mask |= self._bit(atom)
            else:
                self.goal_pos_mask |= self._bit(atom)

        self.ground_actions = self._ground_all()

    # -- parsing (its own grammar implementation) ------------------------- #

    def _literals(self, raw):
        if isinstance(raw, dict):
            items = [(x, False) for x in raw.get("pos", [])]
            items += [(x, True) for x in raw.get("neg", [])]
        else:
            items = [(x, None) for x in raw]
        parsed = []
        for text, forced_neg in items:
            match = self._TOKEN.match(text.replace(" ", ""))
            assert match, f"oracle cannot parse {text!r}"
            pred = match.group(1)
            args = tuple(match.group(2).split(",") if match.group(2) else [])
            neg = text.strip().startswith("~") or forced_neg is True
            parsed.append(((pred, *args), neg))
        return parsed

    def _bit(self, atom):
        if atom not in self.fact_index:
            self.fact_index[atom] = len(self.facts)
            self.facts.append(atom)
        return 1 << self.fact_index[atom]

    def _mask(self, templates, binding):
        mask = 0
        for template, _neg in templates:
            atom = tuple(binding.get(t, t) for t in template)
            mask |= self._bit(atom)
        return mask

    def _ground_all(self):
        grounded = []
        for name, params, pre, add, delete, cost in self.schemas:
            for combo in itertools.permutations(self.objects, len(params)):
                binding = dict(zip(params, combo))
                pre_pos = self._mask([(a, n) for a, n in pre if not n], binding)
                pre_neg = self._mask([(a, n) for a, n in pre if n], binding)
                addm = self._mask(add, binding)
                delm = self._mask(delete, binding)
                grounded.append((name, combo, pre_pos, pre_neg, addm, delm, cost))
        return grounded

    # -- search ----------------------------------------------------------- #

    def _goal(self, state):
        return (state & self.goal_pos_mask) == self.goal_pos_mask and not (
            state & self.goal_neg_mask
        )

    def _successors(self, state):
        for name, args, pp, pn, addm, delm, cost in self.ground_actions:
            if (state & pp) == pp and not (state & pn):
                yield (state & ~delm) | addm, (name, list(args), cost)

    def dijkstra(self):
        """Return (verdict, optimal_cost, plan) over the full reachable set."""
        import heapq

        dist = {self.initial_mask: 0}
        prev = {}
        heap = [(0, self.initial_mask)]
        while heap:
            g, state = heapq.heappop(heap)
            if g != dist[state]:
                continue
            if self._goal(state):
                plan = []
                cursor = state
                while cursor in prev:
                    pstate, action = prev[cursor]
                    plan.append(action)
                    cursor = pstate
                plan.reverse()
                return "solved", g, plan
            for nxt, action in self._successors(state):
                ng = g + action[2]
                if ng < dist.get(nxt, 10**18):
                    dist[nxt] = ng
                    prev[nxt] = (state, action)
                    heapq.heappush(heap, (ng, nxt))
        return "unsolvable", None, []

    def bfs_hops(self):
        from collections import deque

        seen = {self.initial_mask}
        queue = deque([(self.initial_mask, 0, {})])
        while queue:
            state, depth, prev = queue.popleft()
            if self._goal(state):
                return depth
            for nxt, action in self._successors(state):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, depth + 1, {**prev, nxt: (state, action)}))
        return None


# --------------------------------------------------------------------------- #
# Helpers to run the service kernel on the same requests
# --------------------------------------------------------------------------- #

def _service_grounded(request):
    domain = parse_domain(request["domain"])
    problem = parse_problem(request["problem"], domain)
    validate(domain, problem)
    return ground(domain, problem)


def _service_search(request, algorithm, heuristic):
    gp = _service_grounded(request)
    # Generous wall-clock bound so exhaustive comparison results do not
    # depend on machine speed or coverage instrumentation; verdict bounds
    # themselves are tested separately with deliberately tiny limits.
    return gp, search(gp, SearchConfig(algorithm=algorithm,
                                       heuristic=heuristic,
                                       time_limit_seconds=120.0))


def _request_from_files(domain_file, problem_file):
    return {
        "domain": json.loads((FIXTURES / domain_file).read_text()),
        "problem": json.loads((FIXTURES / problem_file).read_text()),
    }


# --------------------------------------------------------------------------- #
# Fixture-driven acceptance: verdicts and costs must match the oracle
# --------------------------------------------------------------------------- #

FIXTURE_CASES = [
    ("domain_resource_ops.json", "problem_solvable.json"),
    ("domain_resource_ops.json", "problem_unsolvable_missing.json"),
    ("domain_resource_ops.json", "problem_unsolvable_sealed.json"),
    ("domain_resource_ops.json", "problem_cycles.json"),
    ("domain_resource_ops.json", "problem_cost_paths.json"),
    ("domain_route_graph.json", "problem_route_cost.json"),
]


@pytest.mark.parametrize("domain_file,problem_file", FIXTURE_CASES)
def test_kernel_verdict_and_optimal_cost_match_independent_oracle(
    domain_file, problem_file
):
    request = _request_from_files(domain_file, problem_file)
    oracle = IndependentOracle(request)
    verdict, opt_cost, opt_plan = oracle.dijkstra()

    for algorithm, heuristic in [(UCS, "zero"), (ASTAR, "h_max")]:
        gp, result = _service_search(request, algorithm, heuristic)
        assert result.status == verdict
        if verdict == "solved":
            assert result.cost == opt_cost
            assert result.optimal_guarantee is True
            report = execute_plan(
                gp,
                [{"name": a.schema_name, "args": list(a.args)}
                 for a in result.plan],
            )
            assert report.valid is True
            assert report.total_cost == opt_cost


def test_bfs_matches_oracle_fewest_hop_count():
    request = _request_from_files(
        "domain_route_graph.json", "problem_route_cost.json"
    )
    oracle_hops = IndependentOracle(request).bfs_hops()
    _gp, result = _service_search(request, BFS, "zero")
    assert result.path_length == oracle_hops == 1
    # And its cost accounting still sums action costs honestly (5, not 1).
    assert result.cost == 5


def test_oracle_plan_and_kernel_plan_are_independently_validated():
    # The oracle's own plan for the solvable fixture must be executable by
    # the service executor: cross-validation in both directions.
    request = _request_from_files(
        "domain_resource_ops.json", "problem_solvable.json"
    )
    oracle = IndependentOracle(request)
    verdict, _cost, oracle_plan = oracle.dijkstra()
    assert verdict == "solved"
    gp = _service_grounded(request)
    report = execute_plan(
        gp, [{"name": n, "args": args} for n, args, _c in oracle_plan]
    )
    assert report.valid is True
    assert report.total_cost == sum(c for _n, _a, c in oracle_plan)


# --------------------------------------------------------------------------- #
# Random-instance differential fuzzing
# --------------------------------------------------------------------------- #

PREDICATES = ("P", "Q", "R")
BUCKETS = ("pre_pos", "pre_neg", "add", "delete", "none")


def _random_request(rng):
    objects = [f"o{i}" for i in range(rng.randrange(2, 4))]
    n_actions = rng.randrange(2, 7)
    actions = []
    for i in range(n_actions):
        assignment = {pred: rng.choice(BUCKETS) for pred in PREDICATES}
        action = {
            "name": f"act{i}",
            "parameters": ["?x"],
            "preconditions": {"pos": [], "neg": []},
            "add": [],
            "delete": [],
            "cost": rng.choice([1, 1, 2, 4]),
        }
        for pred, bucket in assignment.items():
            literal = f"{pred}(?x)"
            if bucket == "pre_pos":
                action["preconditions"]["pos"].append(literal)
            elif bucket == "pre_neg":
                action["preconditions"]["neg"].append(literal)
            elif bucket == "add":
                action["add"].append(literal)
            elif bucket == "delete":
                action["delete"].append(literal)
        actions.append(action)

    all_facts = [f"{p}({o})" for p in PREDICATES for o in objects]
    init_facts = [f for f in all_facts if rng.random() < 0.3]
    goal_facts = rng.sample(all_facts, rng.randrange(1, min(3, len(all_facts))))
    goal = {"pos": [f for f in goal_facts if rng.random() < 0.7],
            "neg": []}
    goal["neg"] = [f for f in goal_facts if f not in goal["pos"]]
    if not goal["pos"] and not goal["neg"]:
        goal["pos"] = goal_facts[:1]

    return {
        "domain": {"name": "random", "actions": actions},
        "problem": {"name": "random-p", "objects": objects,
                    "init": init_facts, "goal": goal},
    }


@pytest.mark.parametrize("seed", range(40))
def test_random_instances_match_oracle(seed):
    import random

    rng = random.Random(seed)
    request = _random_request(rng)
    oracle = IndependentOracle(request)
    verdict, opt_cost, opt_plan = oracle.dijkstra()

    gp, ucs = _service_search(request, UCS, "zero")
    assert ucs.status == verdict
    _, astar = _service_search(request, ASTAR, "h_max")
    assert astar.status == verdict

    if verdict == "solved":
        assert ucs.cost == opt_cost
        assert astar.cost == opt_cost
        # Admissibility: h_max at the initial state never overestimates.
        h0 = MaxHeuristic(gp).value(gp.initial)
        assert h0 <= opt_cost
        # Both returned plans survive independent step-by-step execution.
        for result in (ucs, astar):
            report = execute_plan(
                gp,
                [{"name": a.schema_name, "args": list(a.args)}
                 for a in result.plan],
            )
            assert report.valid is True
            assert report.total_cost == result.cost == opt_cost
    else:
        assert ucs.plan == () and astar.plan == ()
