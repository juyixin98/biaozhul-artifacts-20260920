# Semantic Contract

This document is the normative specification implemented by `tplan`. The test
suite asserts each rule below against an independent brute-force oracle
(`tests/oracle.py`), not only against the kernel itself.

## 1. Time grid

* Time is the finite integer grid `0, 1, ..., H` where `H` is the `horizon`.
* Every action duration and event time is an integer on this grid.
* State changes (effects) occur only at event instants; between them the
  state is constant.

## 2. Fluents and state

* A **fluent** is a named variable holding an exact rational (`Fraction`).
  Initial values come from the problem's `fluents` map. No float arithmetic is
  used anywhere, so `1/3 + 1/3 + 1/3 == 1` exactly.
* Conditions are:

  ```json
  {"fluent": {"id": "x", "op": ">=", "value": 3}}
  {"all": [ ... ]}
  {"any": [ ... ]}
  {"not": { ... }}
  ```

  Operators: `== != < <= > >=`. `all` of an empty list is true; `any` of an
  empty list is false.

## 3. Actions

An action has:

| field | meaning | check instant |
|---|---|---|
| `duration` | non-negative integer grid length | — |
| `start_condition` | conjuncts that must hold to start | at `start`, **before** any of the action's effects |
| `invariant` | conjuncts that must hold while alive | **every** grid point alive |
| `effects` | fluent updates applied at the end | at `end` |
| `resource_use` | resource amounts required while alive | see §6 |

Effect operators: `+ - = *` with an exact-rational amount.

### 3.1 Durational invariants are not endpoint-only

For a positive-duration action alive over the half-open interval
`[start, end)`, the invariant is evaluated at **every** grid point
`start, start+1, ..., end-1` — including interior points. An effect of another
action ending midway through can therefore violate an invariant even when
both endpoints looked fine. The simulator performs an independent per-point
audit (`_audit_invariants`) in addition to the event path.

### 3.2 Zero-duration actions

An action with `duration = 0` starts and ends at the same instant `t`. At that
single instant, in order:

1. start condition checked against the pre-tick state,
2. resources debited,
3. effects applied,
4. resources released,
5. invariant checked against the **post-effect** state.

A zero-duration action therefore occupies no half-open resource interval
(debit and release cancel at the instant) but can still overdraw a consumable
stock at that instant.

## 4. Effects and the end instant

Effects of a positive-duration action apply exactly at `end` (after the
invariant check at `end-1`). They are visible to an action starting at the
same `end` instant because of the event ordering in §5.

## 5. Simultaneous events: explicit policy

At any one instant events are resolved in a deterministic total order:

1. **END** events (effects + resource release), phase 0,
2. **TICK** events (zero-duration actions), phase 1,
3. **START** events (start condition + resource debit), phase 2,

with ties broken by action declaration order.

Concrete rules:

* An `END` at `t` followed by a different action's `START` at `t` is the
  supported **boundary hand-off**: the ending action releases resources and
  applies effects first, so the starter observes the new state. This is
  always legal.
* Two STARTs at the same instant, two ENDs at the same instant, or a TICK
  colliding with any other START/END event, are **rejected** with
  `simultaneous_conflict` rather than resolved ambiguously.

## 6. Resources: half-open occupancy

* **Renewable** resources have a `capacity`. An action using `q` units
  contributes `q` to occupancy on exactly the half-open interval
  `[start, end)`; usage drops at `end`, so a back-to-back action starting at
  `end` reuses the freed capacity (boundary release). An occupancy strictly
  greater than capacity at any grid point is `resource_conflict`.
* **Consumable** resources start at `capacity`; `q` units are debited at
  `start` and credited back at `end`. A debit that would make the stock
  negative is `resource_conflict` at that start instant.

## 7. Goal and plan acceptance

* A schedule reaches the goal only when the goal condition holds in the
  state after **all** scheduled actions have completed (completion
  semantics). A goal that briefly holds while an action is still running is
  not accepted, because pending end effects could invalidate it.
* Plan cost is the number of action instances; ties prefer the earliest
  completion time.

## 8. Search, budget and optimality

* The solver is a depth-first forward search over grid instants, branching on
  "start one admissible action" or "idle", with start-condition, capacity and
  invariant pruning. Each node carries its own counters and end table, so
  backtracking cannot leak state between branches.
* Budgets: a node-count limit and/or a wall-clock deadline.
  * Exhausting the search space with a verified plan ⇒ `optimal`.
  * Exhausting the space with no plan ⇒ `unsat` / `unsat_proven`.
  * Budget reached while holding a feasible plan ⇒
    `feasible_not_proven_optimal` / `budget_exhausted`; the concrete plan is
    returned and optimality is explicitly **not** claimed.
  * Budget reached before any plan ⇒ `budget_exhausted` (no plan asserted).
* Every accepted plan is replayed through the independent simulator before
  it leaves the solver; the service replays it again to materialise the
  evidence timeline. A plan that fails replay surfaces as `replay_mismatch`,
  never as success.

## 9. Explicit failure categories

`invalid_problem`, `start_condition_violated`, `invariant_violated`,
`resource_conflict`, `simultaneous_conflict`, `unsat_proven`,
`budget_exhausted`, `replay_mismatch`, `internal_error`. Unknown or
exceptional states are reported as such (HTTP 422/409/500 with a
`failure_code`), never collapsed into a success response.
