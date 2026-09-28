# API request examples

All endpoints return the envelope:

```json
{
  "success": true,
  "data": { ... },
  "error": null,
  "meta": {"request_id": "...", "service_version": "1.0.0", "endpoint": "..."}
}
```

Start the server first:

```bash
STRATBLOCK_DB=demo/demo.db python3 -m uvicorn stratblock.api:create_app \
  --factory --host 127.0.0.1 --port 8080
```

Default tokens (override with `STRATBLOCK_API_TOKENS`):

| Role          | Token        | Capabilities                                              |
|---------------|--------------|-----------------------------------------------------------|
| enroller      | `enrol-token`| enroll; sees only the assigned arm                        |
| auditor       | `audit-token`| read audit/diagnostics/replay/effect; seed is redacted    |
| administrator| `admin-token`| register/seal studies, enter outcomes, sees the seed      |

## 1. Register a study (administrator)

```bash
curl -s -X POST localhost:8080/v1/studies \
  -H "Authorization: Bearer admin-token" -H "Content-Type: application/json" \
  -d '{
    "study_id": "demo-permuted",
    "arms": ["control", "treatment"],
    "stratification_factors": ["site"],
    "block_sizes": [2, 4],
    "allocation_ratio": [1, 1],
    "tail_policy": "permuted"
  }'
```

`block_sizes` are the sizes the random stream may choose from per block;
each must realise `allocation_ratio` exactly. `tail_policy` is one of
`permuted` or `balanced_prefix`.

## 2. Enroll a subject (enroller) — allocation is concealed

```bash
curl -s -X POST localhost:8080/v1/studies/demo-permuted/enroll \
  -H "Authorization: Bearer enrol-token" -H "Content-Type: application/json" \
  -d '{"subject_id": "P000", "features": {"site": "A"}, "request_id": "req-a-000"}'
```

The enroller response contains **only** the arm. Block size, position,
permutation, and the master seed are never returned to this role.

## 3. Repeat request returns the same allocation (idempotent)

```bash
curl -s -X POST localhost:8080/v1/studies/demo-permuted/enroll \
  -H "Authorization: Bearer enrol-token" -H "Content-Type: application/json" \
  -d '{"subject_id": "P000", "features": {"site": "A"}, "request_id": "req-repeat"}'
# -> same arm, "replayed": true, no new randomness consumed
```

## 4. Feature-changing repeat is refused (HTTP 409)

```bash
curl -s -X POST localhost:8080/v1/studies/demo-permuted/enroll \
  -H "Authorization: Bearer enrol-token" -H "Content-Type: application/json" \
  -d '{"subject_id": "P000", "features": {"site": "B"}, "request_id": "req-change"}'
# -> error.category = "DUPLICATE_CONFLICT", original allocation retained
```

## 5. Diagnostics / correctness report (auditor)

```bash
curl -s localhost:8080/v1/studies/demo-permuted/diagnostics \
  -H "Authorization: Bearer audit-token"
```

Includes per-stratum block counts, two-way sequence replay (production
kernel + independent reference oracle), stream key fingerprints,
distribution checks, and separate `failures` / `uncertainties` lists.
The `master_seed` is redacted for auditors.

## 6. Audit-event replay (auditor)

```bash
curl -s -X POST localhost:8080/v1/studies/demo-permuted/replay \
  -H "Authorization: Bearer audit-token"
```

Re-derives every arm from the append-only audit events and diffs against
what was recorded (`conclusion`: PASS/FAIL).

## 7. Seal enrollment (administrator)

```bash
curl -s -X POST localhost:8080/v1/studies/demo-permuted/seal \
  -H "Authorization: Bearer admin-token"
```

After sealing, new subjects get `STRATUM_CLOSED` (409); repeat requests
for existing subjects still return their frozen arm.

## 8. Outcomes and the (scoped) effect report

```bash
curl -s -X POST localhost:8080/v1/studies/demo-permuted/outcomes \
  -H "Authorization: Bearer admin-token" -H "Content-Type: application/json" \
  -d '{"subject_id": "P000", "y": 0.12}'

curl -s "localhost:8080/v1/studies/demo-permuted/effect" \
  -H "Authorization: Bearer audit-token"
```

The effect report carries `"proves_allocation_correct": false` with an
explicit reason: post-allocation significance addresses the estimand, not
the randomisation mechanism.

## Error categories

`VALIDATION_ERROR`, `UNKNOWN_STUDY`, `UNKNOWN_SUBJECT`,
`DUPLICATE_CONFLICT`, `STRATUM_CLOSED`, `FORBIDDEN`,
`UNAUTHENTICATED`, `REPLAY_MISMATCH`, `INDETERMINATE`, `CONFLICT`.
