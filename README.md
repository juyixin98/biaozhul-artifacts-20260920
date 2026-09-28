# Finite HTN Task Planner

A multi-module backend implementing **finite Hierarchical Task Network (HTN)
planning** with:

- **method selection** over mutually-exclusive decomposition methods, selected
  against the *current* state with full backtracking;
- **sequential and partial-order** method bodies, where partial-order
  feasibility is decided by state simulation, **not** by topological sorting;
- **recursive decomposition** bounded by an explicit depth limit and an
  expansion budget, with active-frame cycle detection;
- **real precondition verification** of every primitive action against a state
  of ground facts and finite shared resources;
- a retained **expansion tree from abstract tasks down to primitive actions**;
- **typed failure-branch evidence** when no method can produce an executable
  plan;
- a **FastAPI** query interface and a **SQLite** evidence store.

All data is local and synthetic. No production accounts or real business data
are used anywhere.

---

## 1. Repository layout

```
src/htn_planner/
    models.py          # typed domain/problem/result models (framework-free)
    rule_language.py   # YAML/JSON rule parser + parameter binding
    state.py           # fact set + finite resources, real precondition checks
    planner.py         # planning kernel (recursion, selection, partial order)
    storage.py         # SQLite evidence store (plans, tree, failures, audit)
    service.py         # orchestration: registry -> kernel -> store + logging
    api.py             # FastAPI query interface
    config.py          # environment-overridable settings
fixtures/
    domains/*.yaml     # synthetic logistics + assembly domains
    problems/*.yaml    # feasible and intentionally-infeasible problems
tests/
    oracle.py          # INDEPENDENT simulator (does not use the planner)
    fixture_loader.py
    test_rule_language.py  (unit)
    test_state.py          (unit)
    test_planner_kernel.py (kernel)
    test_storage_service.py(integration, SQLite)
    test_api.py            (e2e, FastAPI TestClient)
verification/
    run_verification.py    # standalone acceptance harness
scripts/
    run_api.py             # uvicorn entry point
```

The rule language, kernel, evidence store, and query interface are separate
modules; none of the planning behaviour is a hard-coded demo table.

---

## 2. Running

Python 3.12. Install pinned dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Run the tests:

```bash
python3 -m pytest                      # all 51 tests
python3 -m pytest -m kernel            # one category: unit|kernel|integration|e2e
```

Run the standalone acceptance verification:

```bash
python3 verification/run_verification.py
```

Run the HTTP service locally:

```bash
PYTHONPATH=src python3 scripts/run_api.py --host 127.0.0.1 --port 8000
```

Environment overrides: `HTN_DB_PATH`, `HTN_FIXTURE_DIR`, `HTN_LOG_LEVEL`.

Example request:

```bash
curl -s -X POST http://127.0.0.1:8000/plan/fixture \
  -H 'content-type: application/json' \
  -d '{"problem":"logistics_via_hub","request_id":"demo-1"}'
```

Endpoints: `GET /health`, `GET /domains`, `POST /plan/fixture`,
`POST /plan/inline`, `GET /plans`, `GET /plans/{id}`,
`GET /plans/{id}/tree`, `GET /plans/{id}/failures`, `GET /plans/{id}/audit`.

---

## 3. Rule language (boundary semantics)

A small YAML/JSON language. Arguments are constants or `?variables`.

| Form | Meaning |
|------|---------|
| `(pred, args...)` | fact literal that must hold |
| `["not", pred, args...]` | literal must be absent |
| `["avail", RESOURCE, n]` | resource must have ≥ n free units |
| `["bound", RESOURCE, n]` | resource must have < n free units |
| `["bind", "?v", pred, pattern...]` | deterministic lookup: exactly one fact `(pred, *pattern, value)` must exist; binds `?v := value` |

- A **primitive** has `parameters`, a `precondition` list, and an `effect`
  (`add`/`remove` facts, `reserve`/`release` resource units).
- A **method** has `parameters`, a `guard`, and `subtasks`. `order` is
  `sequential` (default) or `partial`; partial subtasks use `after: [ids]`.
- `after` may only reference **earlier-declared siblings**, so the sibling DAG
  is acyclic by construction; forward references and duplicate ids are rejected
  at parse time.

### Important semantic boundaries

1. **Topological order ≠ executability.** A partial-order body whose DAG has a
   topological sort is not automatically feasible. The kernel interleaves only
   *ready* siblings and simulates each prefix against the state; an ordering
   whose next action's precondition fails is discarded and another ready
   sibling is tried. The fixtures `two_consume` (single non-replenishable fact)
   and `two_grab` (capacity-1 resource, no release) both have valid topological
   orders and no executable one, and are reported
   `partial_order_infeasible`.
2. **Recursion is bounded.** Deeper frames than `max_depth` produce
   `depth_exceeded`; method applications beyond `max_expansions` produce
   `expansion_budget_exhausted`. These are hard bounds and propagate to the
   terminal result rather than being retried as ordinary alternatives.
3. **Cycle detection uses the state signature.** The same task frame recurring
   with an unchanged fact/resource signature cannot make progress and is
   reported `method_cycle`. Recursion that *does* change state (the `haul`
   route growing toward the goal) is legitimate and is not flagged.
4. **Deterministic binding only.** `bind` requires exactly one matching fact;
   zero or several matches reject the method rather than guessing a value.
5. **Method guards see the current state.** Two methods on one task can be
   mutually exclusive (e.g. `haul` direct when connected, via-hub otherwise).
6. **Resources are finite.** `reserve` beyond free capacity fails the action;
   capacity is never silently exceeded.

### Failure categories

`unresolvable_task`, `guard_rejected`, `precondition_not_stat`,
`resource_unavailable`, `depth_exceeded`, `expansion_budget_exhausted`,
`method_cycle`, `partial_order_infeasible`, `no_viable_method`.

For an infeasible plan, `failures` carries the terminal category plus every
rejected branch. For a *feasible* plan, branches explored and abandoned during
backtracking are returned separately under `abandoned_branches`, and a note is
placed in `uncertainty` — conclusions that are not clean "pass" facts are
listed there rather than presented as verified.

---

## 4. Why the results are trustworthy

- Expected answers in tests are **hand-derived literals** (exact grounded
  action sequences, exact chosen methods, exact failure categories).
- A returned plan is additionally replayed by `tests/oracle.py`, an
  **independent simulator** that parses the raw YAML itself and maintains its
  own fact/resource bookkeeping; it imports neither the planner nor its state.
  Thus the reference answers are not generated by the implementation under
  test.
- After construction, the kernel itself runs a second independent validation
  pass over the retained tree (re-simulation + partial-order edge checks).
- Tests assert specific outcomes and failure kinds — not merely that an
  endpoint responds.

---

## 5. Explainability

Every run is keyed by a **request id** (client-supplied or generated). Stored
evidence includes: the service name/version, domain version, ordered key-step
**audit log** with processing locations (`service:*`, `kernel:*`), the chosen
method at each selection point, rejected guards, the terminal failure, and
abandoned branches. Failures and uncertain conclusions are always returned in
their own fields, never folded into a success result.

---

## 6. Checks that were NOT executed

None at delivery time: all 51 automated tests pass and
`verification/run_verification.py` reports `not_executed=0`. The harness keeps
an explicit `NOT EXECUTED` section by design — if a fixture cannot be loaded or
a check raises before asserting, it is listed there and the process exits
non-zero, rather than being counted as a pass.

### Deliberate non-goals (not implemented, not claimed)

- No temporal/scheduling metric optimisation (durations are parsed but the
  planner returns *an* executable ordering, not a shortest-makespan one).
- No quantified/open-variable search or automated learning; arguments derive
  from call parameters or deterministic `bind` guards only.
- No external services, authentication, or multi-user concerns — this is a
  local, single-tenant backend.
