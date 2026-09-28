# Architecture

```
HTTP (FastAPI, tplan/api.py)
  schemas (tplan/schemas.py)            typed request/response models
        |
Service orchestration (tplan/service.py)
  parse -> Solver.solve -> independent simulate -> persist evidence
        |                          |
Rule language & domain     Planning kernel (tplan/solver.py)
(tplan/conditions.py,       forward DFS, budgets, pruning, replay-check
 tplan/model.py)                    |
        |                  Independent replay kernel (tplan/simulator.py)
        |                  event total order + per-point invariant audit
Configuration (tplan/config.py)      Evidence store (tplan/store.py, SQLite)
```

Layer rules:

* `conditions.py` / `model.py` know nothing about search or HTTP.
* `simulator.py` imports only the domain model; it never imports `solver.py`.
* `solver.py` calls `simulator.simulate` to verify any plan it would return,
  so the search cannot certify itself.
* `tests/oracle.py` imports **only** the parsed domain objects and
  re-derives feasibility with a different algorithm (interval summation and
  a time-stepped loop). Reference answers are never produced by the kernel
  under test.
* `store.py` is the only module that opens SQLite; `service.py` is the only
  module that calls it.
* `api.py` is a thin adapter: validation, dependency wiring, status codes.

## Evidence model (SQLite)

* `runs` — run id, timestamps, status, explicit failure code, budgets,
  nodes expanded, accepted cost/goal time, the exact problem JSON and its
  SHA-256.
* `run_schedule` — ordered accepted schedule.
* `run_timeline` — every replayed event with full state/resource snapshots.
* `run_trace` — search steps: `plan_found`, `prune_start_condition`,
  `prune_resource`, `prune_invariant`, `budget_exhausted`.

## Reproducibility

Every response and every log line carries `run_id` plus the 16-char input
fingerprint; logs additionally record service/Python/FastAPI/Pydantic
versions, node progress vs. budget, elapsed time and the replay verdict.
Pytest writes `logs/test-<utc>-<uuid>.log` per session.
