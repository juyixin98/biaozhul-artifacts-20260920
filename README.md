# Temporal Planner — finite action set with start conditions, durational invariants and end effects

A small, fully local planning service. Given a finite set of durative actions
on a small integer time grid, it finds and verifies schedules and records
complete evidence for every run.

* **Rule language** — exact-rational fluents, comparison / boolean conditions.
* **Planning kernel** — forward search with start conditions, per-point
  durational invariants, end effects, an explicit simultaneous-event policy
  and half-open resource intervals; budget-aware optimality reporting.
* **Independent replay kernel** — reconstructs the whole timeline; the solver
  cannot certify itself.
* **Independent brute-force oracle** (`tests/oracle.py`) — a separate
  implementation used to cross-check failure categories and optima.
* **Evidence store** — SQLite record of inputs, plans, full timelines and
  search traces, keyed by run id and input hash.
* **Query interface** — FastAPI HTTP API and a CLI.

See [`docs/SEMANTICS.md`](docs/SEMANTICS.md) for the normative rules and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for module layout.

## Quickstart

```bash
python3 -m venv .venv && . .venv/bin/activate    # optional
pip install -r requirements.txt                  # pinned, local only

# tests (70 tests, ~2s, coverage gate at 80%)
python3 -m pytest

# API
uvicorn tplan.api:app --host 127.0.0.1 --port 8000
```

Requirements are pinned to the environment this was verified on
(FastAPI 0.141.1, Pydantic 2.13.5, Python 3.12); nothing needs network access
or external accounts at runtime.

## Example: solve

```bash
curl -s 127.0.0.1:8000/api/v1/problems/solve \
  -H 'content-type: application/json' \
  --data @examples/solve_request.json | python3 -m json.tool
```

```jsonc
{
  "run_id": "run-20260927T183541-85127b531716",
  "input_sha256": "c6a920fb8ba59739",
  "status": "optimal",                 // or feasible_not_proven_optimal | unsat | invalid | budget_exhausted
  "failure_code": null,
  "optimal": true,
  "best_cost": 2,
  "goal_time": 5,
  "nodes_expanded": 34,
  "node_budget": 100000,
  "schedule": [
    {"action_id": "carry_a", "start": 0, "end": 3},
    {"action_id": "carry_b", "start": 3, "end": 5}
  ],
  "timeline": [ /* every event with state + resource snapshots */ ],
  "trace":    [ /* plan_found / prune_* / budget_exhausted steps */ ],
  "engine": {"service_version": "1.0.0", "python": "3.12.3", "...": "..."}
}
```

`carry_a` runs on `[0,3)` and the single robot is released exactly at `t=3`
(half-open interval), letting `carry_b` start at `t=3` after observing
`carry_a`'s end effect.

## Example: independent replay of a schedule

```bash
# back-to-back at the half-open boundary -> valid
python3 -m scripts.run_cli replay examples/robot_carry.json \
  --at carry_a:0:3 --at carry_b:3:5

# overlapping by one grid point -> 409/exit 3 with an explicit category
python3 -m scripts.run_cli replay examples/robot_carry.json \
  --at carry_a:0:3 --at carry_b:2:4
```

## Example: budget that cannot prove optimality

```bash
# A tiny node budget returns a feasible plan but does not claim optimality.
curl -s 127.0.0.1:8000/api/v1/problems/solve \
  -H 'content-type: application/json' \
  -d '{"problem": <problem>, "node_budget": 5}'
# status = "feasible_not_proven_optimal", failure_code = "budget_exhausted"
```

## Problem format

```json
{
  "horizon": 6,
  "fluents": {"x": 0, "y": 0},
  "resources": [
    {"id": "robot", "capacity": 1, "kind": "renewable"},
    {"id": "fuel",  "capacity": 3, "kind": "consumable"}
  ],
  "actions": [
    {
      "id": "carry_a",
      "duration": 3,
      "start_condition": [{"fluent": {"id": "x", "op": "<", "value": 5}}],
      "invariant":       [{"fluent": {"id": "x", "op": ">=", "value": 0}}],
      "effects": [{"fluent": "x", "op": "+", "amount": 1}],
      "resource_use": [{"resource": "robot", "amount": 1}]
    }
  ],
  "goal": {"all": [{"fluent": {"id": "y", "op": ">=", "value": 1}}]}
}
```

* Conditions: `{"fluent": {...}}`, `{"all": [...]}`, `{"any": [...]}`,
  `{"not": {...}}`; ops `== != < <= > >=`; values are exact rationals
  (e.g. `"1/3"`).
* Effect ops: `+ - = *`. Durations are non-negative integers (zero allowed).

## What the tests prove

| Question | Where it is answered |
|---|---|
| Exhaustive small-grid reference | `tests/test_oracle_crosscheck.py` vs `tests/oracle.py` |
| Invariant fails **mid-action**, not at endpoints | `test_invariant_checked_at_interior_point_not_only_endpoints` |
| Zero-duration actions | `test_zero_duration_*` |
| Half-open boundary release | `test_half_open_boundary_release_*` |
| Independent replay of the full timeline | `test_full_timeline_*`, service/API evidence tests |
| Specific result **and** failure category asserted | every simulator/solver/API test |
| Budget ⇒ feasible plan, optimality not proven | `test_budget_exhausted_reports_feasible_but_unproven` |
| Proven UNSAT | `test_solver_proves_unsat_by_exhaustion` |

Reference answers come from `tests/oracle.py`, which shares no code with the
solver or simulator.

## Reproducing a failure

1. The test session prints `session_run_id` and the log file
   (`logs/test-<utc>-<uuid>.log`), which records versions first.
2. Each solve returns `run_id` + `input_sha256`; the same pair appears in
   logs. Fetch the full evidence:
   `GET /api/v1/runs/<run_id>` (problem, schedule, timeline, trace).
3. The trace shows the decision steps and the exact pruning reason; replay
   failures carry the failing grid time and the violated condition.
4. Exceptions return `failure_code: internal_error` (HTTP 500) with the
   exception type; they are never reported as success.

## Configuration

Environment variables (all optional, defaults are local):

| variable | default | meaning |
|---|---|---|
| `TPLAN_DB_PATH` | `data/evidence.sqlite3` | SQLite evidence file |
| `TPLAN_NODE_BUDGET` | `100000` | default node limit per solve |
| `TPLAN_TIME_BUDGET_SECONDS` | `10` | default wall-clock limit |
| `TPLAN_MAX_HORIZON` | `200` | largest accepted horizon |
| `TPLAN_MAX_ACTIONS` | `64` | largest accepted action set |
| `TPLAN_MAX_REPEATS` | `64` | per-action start cap in search |
| `TPLAN_LOG_DIR` | `logs/` | log directory |

## Remaining limitations

* **Discrete, small grid.** Time and durations are integers and the search is
  exponential in horizon × action count; the planner is intended for small
  grids (the independent oracle only covers very small ones). The
  `MAX_REPEATS` cap and budgets bound the work.
* **One start per instant.** The simultaneity policy orders an END before a
  START at the same instant but rejects two starts / two ends / tick-event
  collisions instead of merging them.
* **Cost model.** Optimality is minimum action count, then earliest
  completion; action weights / makespan-only objectives are not modelled.
* **No concurrency control** on the SQLite file beyond SQLite's own locking —
  appropriate for local single-process use.
* Effects are assignment/arithmetic on rational fluents; there are no
  symbolic or continuous effects.
