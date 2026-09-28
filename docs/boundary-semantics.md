# Boundary Semantics

This document pins down the exact observable behavior of the service at the
edges where RFC 6902 / RFC 6901 leave room for implementation choices or where
this service deliberately restricts the standard. It is the contract the tests
assert against.

## 1. JSON Pointer (RFC 6901)

- The empty string `""` denotes the **whole document**.
- Tokens are the substrings between `/`. Consecutive slashes produce **empty
  tokens**, which are valid and address object keys that are the empty string.
- Unescaping order is fixed: `~1` → `/`, then `~0` → `~`. Consequences:
  - `/a~1b` addresses key `a/b`.
  - `/c~0d` addresses key `c~d`.
  - `/e~01f` addresses key `e~1f` (the `1` is literal after `~0`).
  - `/g~001h` addresses key `g~01h`.
- A `~` not followed by exactly `0` or `1` is `INVALID_POINTER` (e.g. `/~`,
  `/~2`, `/a~x`).
- A non-empty pointer not beginning with `/` is `INVALID_POINTER`.

## 2. Array indices

- Indices are decimal digits only: `/0`, `/12`.
- Leading zeros are illegal (`/01` → `ARRAY_INDEX_INVALID`), except `/0`.
- `-` denotes **one position past the end** and is only meaningful for `add`
  (append). Reading or removing `/x/-` on an existing array is
  `POINTER_TARGET_MISSING` / `ARRAY_INDEX_OUT_OF_BOUNDS`.
- `add`:
  - `index === length` (and `-`) appends;
  - `0 <= index < length` inserts and shifts existing elements **right**;
  - `index > length` is `ARRAY_INDEX_OUT_OF_BOUNDS`.
- `remove` shifts trailing elements **left**.
- A non-numeric token where an array index is required is
  `ARRAY_INDEX_INVALID`.

## 3. The six operations

| Operation | Required members | Target existence | Notes |
| --- | --- | --- | --- |
| `test` | `path`, `value` | must exist | Deep equality; failure is `TEST_FAILURE` |
| `add` | `path`, `value` | parent must exist | At root replaces the whole document |
| `remove` | `path` | target must exist | Removing root is rejected (`PATH_TYPE_MISMATCH`) |
| `replace` | `path`, `value` | target must exist | At root replaces the whole document |
| `move` | `from`, `path` | `from` must exist | Equivalent to remove `from`, then add to `path` |
| `copy` | `from`, `path` | `from` must exist | Deep-copies the value (no storage aliasing) |

Strict member rules: any member other than `op`, `path`, `value`, `from` is
rejected; `value` is forbidden on `remove`/`move`/`copy`; `from` is forbidden on
`test`/`add`/`remove`/`replace`. These are `MALFORMED_PATCH`.

### `move` descendant rule

`move` with a `path` that is a proper descendant of `from` is rejected with
`MOVE_INTO_DESCENDANT` before any read/removal. `move` to the same path is
allowed and leaves the value in place (remove then add the same value).

### Existence vs. parent-chain categories

To keep failure classification unambiguous, missing locations are categorized
by depth:

- a missing or non-traversable **final** token → `POINTER_TARGET_MISSING`
  (array index cases: `ARRAY_INDEX_OUT_OF_BOUNDS` / `ARRAY_INDEX_INVALID`);
- a missing **intermediate** token in the parent chain →
  `POINTER_PARENT_MISSING`;
- traversal through a scalar/`null`, or adding a child to a scalar/`null`
  parent → `PATH_TYPE_MISMATCH`.

## 4. Evaluation order and atomicity

- Operations are parsed and executed **one at a time, in order**. Each
  operation observes the document produced by the previous one. A runtime
  failure at an earlier index is reported even if a later operation is itself
  malformed.
- On any failure the patch is atomic:
  - the kernel returns the **original input document** (same reference);
  - the input object is never mutated (structural edits share unchanged
    branches);
  - the SQLite transaction is rolled back and the stored version is unchanged.
- The `steps` trace records, for every operation, one of `applied`, `failed`,
  `skipped`. `applied` entries carry the full document state immediately after
  the step; the `failed` entry carries the document state at the point of
  failure (so earlier in-kernel edits are visible for diagnosis even though
  the committed result is rolled back); later operations are `skipped`.

## 5. Versions and concurrency

- Documents start at version `0`. Each successful, **non-empty** patch
  increments the version by exactly one.
- An **empty patch** (`[]`) is a successful no-op and does **not** change the
  version.
- `expectedVersion` is optional:
  - present: it is compared to the current version **inside** an
    `BEGIN IMMEDIATE` transaction before the kernel runs; mismatch →
    `VERSION_CONFLICT`, no operation executes;
  - omitted: the patch applies to whatever the current version is.
- The commit is a guarded `UPDATE ... WHERE version = current`, so two
  version-bound writers cannot both commit against the same base version.

## 6. Failure categories and HTTP status

| Category | HTTP | Meaning |
| --- | --- | --- |
| `MALFORMED_PATCH` | 400 | Patch/op shape is invalid |
| `INVALID_POINTER` | 400 | Pointer string or escape invalid |
| `ARRAY_INDEX_INVALID` | 400 | Token is not a legal array index |
| `BAD_REQUEST_BODY` | 400 | Request body/params invalid |
| `POINTER_TARGET_MISSING` | 422 | Final location does not exist |
| `POINTER_PARENT_MISSING` | 422 | An intermediate container does not exist |
| `ARRAY_INDEX_OUT_OF_BOUNDS` | 422 | Index outside the legal add/remove range |
| `PATH_TYPE_MISMATCH` | 422 | Traversal/parent is a scalar or null |
| `TEST_FAILURE` | 409 | `test` deep-equality failed |
| `MOVE_INTO_DESCENDANT` | 409 | Illegal move target |
| `VERSION_CONFLICT` | 409 | `expectedVersion` does not match |
| `DOCUMENT_NOT_FOUND` | 404 | Unknown document id |
| `AUDIT_RECORD_NOT_FOUND` | 404 | Unknown diagnostics request id |
| `INTERNAL_ERROR` | 500 | Unexpected server-side failure (not a patch contract error) |

## 7. Deliberate non-goals / restrictions

- The full RFC 6902 operation set is intentionally limited to the six required
  operations.
- No authentication/authorization, multi-tenant access control, or network
  exposure beyond the loopback default.
- No HTTP `PATCH` format negotiation (only the documented JSON body).
- Stored documents are validated as JSON values; JSON numbers outside the safe
  range are accepted only as stored data, while index tokens must be safe
  integers.

## 8. Checks intentionally NOT executed (do not read these as passing)

- No load/soak test and no crash-recovery drill after killing the process
  mid-write (SQLite WAL durability is assumed, not separately power-loss
  tested).
- No multi-process or cross-machine concurrency test; the concurrency evidence
  is two writers in one process against one connection's immediate
  transaction.
- No TLS, authentication, or rate-limit verification (out of scope for the
  local service).
- No RFC 6902 official JSON test-suite conformance run against the IETF
  published cases; conformance is evidenced instead by the independent
  reference implementation plus 1000 seeded differential cases.
- No performance benchmark or memory profiling.
