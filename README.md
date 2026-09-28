# Finite-Domain CSP Service

A finite-domain Constraint Satisfaction Problem (CSP) service over **synthetic
variable networks**, built with Python, FastAPI and SQLite. It supports:

- finite **integer domains**;
- **binary relations** given as explicit allowed/forbidden pair tables or
  arithmetic comparisons (`eq, ne, lt, le, gt, ge`);
- the **`all_different` global constraint**;
- domain-propagation **reasons** for every pruned value;
- backtracking search that reports **satisfiable / unsatisfiable / unknown**
  as distinct outcomes;
- durable **evidence storage** of every run and pruning reason.

There is no game UI and no game-themed vocabulary; the output is constraint
solver evidence.

## Project layout

```
app/
  config.py                 runtime configuration (CSP_* env vars)
  solver/
    models.py               rule language: CSPModel / BinaryRelation
    domain.py               mutable domains + explicit trail
    matching.py             Hopcroft-Karp maximum bipartite matching
    propagate.py            AC-3 binary propagation + Regin all-different
    search.py               backtracking search, budgets, status classes
  store/evidence.py         SQLite evidence store
  api/                      FastAPI schemas, service, routes, app factory
  fixtures/catalog.py       reusable synthetic fixtures (plain JSON data)
tests/
  oracle.py                 independent itertools enumeration reference
  test_*.py                 concrete assertions against the oracle
  conftest.py               run-id/version/progress/verdict logging
scripts/
  verify.py                 verification harness (solver vs. oracle)
  run_server.sh             local uvicorn launcher
requirements.txt            exact pinned dependency versions
```

The rule language, inference kernel, evidence store and query interface are
separately organized; tests and configuration are independent layers.

## Algorithm contract

### Binary constraints — AC-3

Binary constraints run an explicit arc-consistency (AC-3) revision loop over
an arc queue. A value without any supporting value in the neighbouring
domain is pruned with a `binary_support` reason (including the neighbour's
current domain).

### all_different — matching + Hall / SCC reasoning (Regin)

`all_different` is **not** implemented as pairwise deletion of assigned
values. For each group the propagator:

1. builds the variable-to-value bipartite graph from the current domains;
2. computes a maximum matching with **Hopcroft-Karp**; if fewer than
   `|variables|` edges match, alternating reachability exposes the
   **Hall-violating subset**, and the group is reported infeasible with
   `failure.kind = "hall_violation"`;
3. when a perfect matching exists, builds the directed residual graph
   (variable→value for admissible edges, value→variable for matching edges),
   computes its **strongly connected components (iterative Tarjan)** and
   alternating reachability from free variable/value nodes, and prunes every
   edge that belongs to no covering matching.

This performs genuine Hall-set inference even when nothing is assigned:
a tight Hall set `{a,b}` over values `{1,2}` causes `1,2` to be removed from
every other variable (see `test_tight_hall_set_pruning_beyond_pairwise_deletion`).

### Backtracking restores all domains and the queue

Search branches by MRV variable selection. Every assignment pruning and every
propagation pruning is appended to a per-branch **trail**; on backtrack the
trail is replayed to restore the exact prior domains. The propagation queue is
local to a single propagation call, so it is restored implicitly when the
branch frame is discarded.

### Statuses are never conflated

- `sat` — a satisfying complete assignment was found (it is returned and
  validated);
- `unsat` — infeasibility was proven, with a failure category
  (`empty_domain` or `hall_violation`);
- `unknown` — the node/backtrack budget was exhausted before deciding; the
  response carries `failure.kind = "budget"` and **never** a fake success.

### Reasons

Each pruned value is recorded with the responsible constraint, a kind
(`binary_support` / `alldifferent_matching`) and a structured `detail`
(support domains, removed edge, matching owner, SCC ids and the exact rule).
Reasons are returned by `/api/solve`, persisted per run, and retrievable from
`/api/runs/{run_id}`.

## Running locally

```bash
pip install -r requirements.txt
bash scripts/run_server.sh            # http://127.0.0.1:8000
```

Useful endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | status and solver version |
| POST | `/api/solve` | solve a model |
| GET | `/api/runs?limit=` | list stored runs |
| GET | `/api/runs/{run_id}` | model, outcome, stats, failure, reasons |
| GET | `/api/fixtures` | list synthetic fixtures |
| GET | `/api/fixtures/{name}` | fetch a fixture payload |

Example request body:

```json
{
  "model": {
    "name": "example",
    "domains": {"a": [1, 2], "b": [1, 2], "c": [1, 2, 3]},
    "binary_constraints": [],
    "all_different": [["a", "b", "c"]]
  },
  "max_nodes": 100000,
  "max_backtracks": 100000,
  "collect_reasons": true
}
```

Configuration is environment-based with `CSP_` prefixes (see
`app/config.py`): `CSP_DATABASE_PATH`, `CSP_MAX_NODES`,
`CSP_MAX_BACKTRACKS`, `CSP_MAX_VARIABLES` (64), `CSP_MAX_DOMAIN_SIZE` (256),
`CSP_LOG_LEVEL`.

## Tests and verification

```bash
python3 -m pytest tests/ -q
python3 scripts/verify.py --random-cases 100 --report verification_report.json
```

### Independent reference oracle

`tests/oracle.py` enumerates **all complete assignments** with
`itertools.product` and imports **nothing from the solver kernel**. The
solver is cross-checked against it:

- root propagation never removes a value that appears in an enumerated
  solution (the soundness property "propagation must not delete a value used
  by any real solution");
- the returned solution satisfies the model and belongs to the enumerated
  solution set;
- `sat`/`unsat` status agrees with complete enumeration.

Tests assert concrete results and failure categories, not merely that an
endpoint responds.

### Fixtures cover the required cases

- **Hall conflict** (`hall_conflict`, `induced_hall_conflict`) — infeasibility
  only matching/Hall reasoning detects; the induced case needs binary
  propagation first;
- **isolated variable** (`isolated_variable`) — an unconstrained variable
  keeps its whole domain;
- **multiple solutions** (`multiple_solutions`, `coloring_three_nodes`) —
  exact enumerated counts (6 and 12);
- **deep backtracking** (`deep_backtrack_sat`: unique solution, ≥10
  backtracks, 3125 assignments; `deep_backtrack_unsat`: zero solutions,
  dozens of backtracks, 288 assignments) plus 4- and 8-queens.

### Logs

Each pytest session writes `tests/logs/test_run_<run_id>.log` containing the
run id, versions (Python, solver, FastAPI, pydantic, Pydantic), per-test
input identity (fixture/model names), start/end progress, and the concrete
basis recorded for each verdict. Exceptions and `unknown` states surface as
failures/`unknown`; they are never reported as success.

## Boundary semantics and checks not executed

- **Inference strength.** Binary propagation is arc consistency and
  all-different is Regin's domain consistency. Neither is guaranteed to
  decide every instance at the root; search (with a budget) handles the rest.
- **Completeness vs. budget.** When the budget is exhausted the answer is
  honestly `unknown`, not `sat`/`unsat`.
- **Enumeration limit.** The queens-8 fixture has an 8**8 = 16,777,216
  assignment space, so full enumeration is **not executed** for it. The
  verification report records these as `skipped` (`full_enumeration`,
  `root_propagation_soundness`); queens-8 is instead checked for a directly
  validated solution (distinct rows, no diagonal clash). They are reported
  as skipped, never as passed.
- **Service limits.** Requests are bounded to 64 variables and 256 values
  per domain (HTTP 422 above the limits); budgets cap search effort.
- **Local data only.** All inputs are synthetic local fixtures; the SQLite
  store is a local file; no production accounts or external services are
  used.
- **Integer domains.** Domain values are integers; other value types are out
  of scope.

## Dependency versions

See `requirements.txt` for exact pins (FastAPI 0.141.1, Pydantic 2.13.5,
uvicorn 0.54.0, httpx 0.28.1, pytest 9.1.1, and transitive dependencies).
