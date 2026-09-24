# Storage migration: failure & recovery records

`cmd/storage-migrate` rewrites every Task so etcd stores v1 bytes. It never
fails silently: each object's outcome is appended, one JSON value per line
(JSONL), to `--record-file`. A non-zero exit means the run was incomplete and
the same command can be replayed — updates are idempotent.

## Record schema

```json
{
  "time": "RFC3339 UTC timestamp",
  "name": "object name",
  "namespace": "namespace",
  "fromVersion": "version the object was served as during the read",
  "toVersion": "v1",
  "status": "migrated | skipped | failed",
  "reason": "already-stored-as-v1 | dry-run | conflict | update-error | ...",
  "httpStatus": 409,
  "error": "verbatim API server error when failed",
  "resourceName": {"Namespace": "…", "Name": "…"}
}
```

## Authentic example: crash after the first object, then resume

`test/failure-records/example-records.jsonl` was produced by real CLI runs
against an envtest cluster (`--fail-after 1` to inject the crash, then a plain
re-run). It shows the three happy-path statuses:

1. `gen-a` migrated in run 1, then the process exited non-zero as instructed.
2. Run 2 **skipped** `gen-a` (`already-stored-as-v1`) — re-running is safe and
   does not double-migrate.
3. `gen-b` and `gen-c` migrated in run 2. Exit code 0.

`--fail-after` is a test hook; real interruptions (SIGKILL, network partition)
leave exactly the same partial file and are recovered the same way: just
re-run.

## Authentic example: failed update (admission denial)

`test/failure-records/example-denied-records.jsonl` contains a genuine failed
record. A validating webhook in that environment forbade Task updates; the CLI
recorded:

```json
{"…":"…","name":"gen-denied","status":"failed","reason":"update-error",
 "httpStatus":403,
 "error":"admission webhook \"fixture-deny.migration.example.io\" denied the
  request: fixture deny webhook: storage migration updates are frozen"}
```

Run exit code was 1 with:

```
MIGRATION INCOMPLETE: 1 object(s) failed; see <record-file> and re-run
```

The already-migrated objects appeared as `skipped` in the same file, so the
file doubles as a progress log and an incident record.

## Recovery procedure

1. Read the failed lines:
   ```sh
   grep '"status":"failed"' migration-records.jsonl
   ```
2. Resolve the underlying cause (admission policy, RBAC, conflict churn,
   webhook outage). Conflicts (`reason":"conflict"`, httpStatus 409) need no
   special action — they self-heal on re-run.
3. Re-run the identical command. `migrated` objects become `skipped`; failed
   ones are retried.
4. Only a fully successful run trims `status.storedVersions` on the CRD to
   `[v1]` (disable with `--trim-stored-versions=false`). Never remove
   v1alpha1 from served versions while failed records remain.
5. `--dry-run` performs the read/conversion side only and records
   `reason":"dry-run"` without calling update — use it to preview whether any
   object is inexpressible before the real cut-over.

## Status/exit codes

| Exit | Meaning                                                        |
|------|----------------------------------------------------------------|
| 0    | all objects migrated/skipped, storedVersions trimmed if asked |
| 1    | at least one object failed (or the injected `--fail-after` fired) |
| 2    | objects migrated but storedVersions could not be trimmed      |
