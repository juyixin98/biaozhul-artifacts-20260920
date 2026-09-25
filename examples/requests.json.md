# Request bodies for POST /jobs

## admitted.json — an honest job that can run now
{
  "id": "a",
  "payload": "sleep:300",
  "demand": 1,
  "deadline_rel_ms": 2000,
  "budget_ms": 300
}

## queued.json — admitted but waits for capacity (single machine busy)
{
  "id": "b",
  "payload": "sleep:200",
  "demand": 1,
  "deadline_rel_ms": 5000,
  "budget_ms": 200
}

## overrun.json — declares 100 ms but actually runs 600 ms; with
## -overrun=kill_at_budget it is killed at 100 ms and counted as a timeout
{
  "id": "over",
  "payload": "sleep:600",
  "demand": 1,
  "deadline_rel_ms": 5000,
  "budget_ms": 100
}

## infeasible.json — conservative admission rejects this up front (HTTP 422):
## budget 1000 ms cannot fit in the 800 ms until the deadline even if started now
{
  "id": "x",
  "payload": "sleep:1000",
  "demand": 1,
  "deadline_rel_ms": 800,
  "budget_ms": 1000
}

## absolute_deadline.json — deadline_ms instead of deadline_rel_ms
## (absolute Unix milliseconds)
{
  "id": "abs",
  "payload": "sleep:50",
  "demand": 1,
  "deadline_ms": 1800000000000,
  "budget_ms": 50
}
