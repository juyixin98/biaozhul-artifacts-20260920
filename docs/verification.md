# Verification Guide

This is the acceptance evidence for the service. Every check below is
reproducible from a clean checkout with only local, synthetic data.

## One command

```bash
npm run verify        # scripts/verify.sh
```

Stages, in order:

1. dependency availability check (uses `npm ci` when a lockfile exists);
2. `tsc --noEmit` typecheck;
3. Vitest run with v8 coverage and an enforced 80% threshold;
4. `tsc -p tsconfig.build.json` emit build;
5. boot the **real** server against an isolated temporary SQLite database;
6. run `scripts/e2e-scenario.mjs`, which asserts concrete results over HTTP;
7. print request-correlated JSON log lines and tear the server down.

The script fails fast (`set -euo pipefail`) and removes its temp directory on
exit.

## Last recorded result (this environment)

- Typecheck: clean.
- Tests: **74 passed** across 6 files:
  - `pointer` 17, `kernel` 27, `service` 9, `http` 12, `differential` 6
    (one of which expands to **1000 seeded generated cases**), `config` 3,
    plus shared fixture-pair cases.
- Coverage (v8), all above the 80% gate:
  - statements **92.78%**, branches **87.68%**, functions **97.14%**,
    lines **92.78%**.
- Build: emitted to `dist/`.
- End-to-end HTTP scenario: **20 concrete checks passed**, including exact
  post-patch documents, unchanged version after a mid-patch failure,
  `VERSION_CONFLICT`, `MOVE_INTO_DESCENDANT`, and diagnostics correlation.

These numbers are from the run in this environment; rerun `npm run verify` to
regenerate them rather than trusting the prose.

## How atomicity is actually evidenced

- **Kernel level** (`test/kernel.test.ts`): a patch with three applied ops, a
  failing `test`, and a trailing op asserts that
  - the returned result is deep-equal *and reference-identical* to the input,
  - the input object was not mutated,
  - step statuses are exactly
    `applied / applied / applied / failed / skipped`,
  - the `failed` step's `documentAt` shows the earlier in-kernel edits while
    the final committed result is the original (diagnosis vs. commit are
    distinct).
- **Persistence level** (`test/service.test.ts`): after the same shape of
  failure the stored row is byte-identical and still at version `0`.
- **HTTP level** (`test/http.test.ts`, `scripts/e2e-scenario.mjs`): a
  subsequent `GET /documents/:id` shows the pre-patch document and the old
  version, and the audit record for the failed request has `toVersion = null`.

## How the independent comparison works

`test/reference/naiveReference.ts` is a second, independently written
implementation. It is not produced by `src/`:

- it `structuredClone`s input and **mutates in place** (the kernel rebuilds
  immutable structures sharing unchanged branches);
- it walks pointers recursively with a positional cursor and unescapes tokens
  with a regex callback (the kernel uses index loops);
- it raises its own local error tags, mapped to public categories through an
  explicit table.

`test/differential.test.ts` feeds both implementations the five shipped
fixture pairs and 1000 seeded, generated (document, operation-sequence) cases.
For every case it asserts agreement on success/failure, the exact result
document, the failure category, and the failed-at index.

### Real defects the cross-check surfaced during development

These were genuine disagreements found and fixed — evidence the comparison has
teeth:

1. **Kernel: adding a child to a scalar parent was silently accepted.** A path
   like `/x/child` where `x` is a number produced `{...42}` into an object
   instead of `PATH_TYPE_MISMATCH`. Fixed by validating the immediate
   container's type in `locateParent`.
2. **Reference: root-level `add`/`replace` was dropped.** The in-place model
   returned a new root that the caller did not rebind, so a root replacement
   was ignored. Fixed in the reference by rebinding its document reference.

## Request correlation and diagnostics

- Every request has an id (`X-Request-Id` / body `requestId`, or generated).
- The JSONL log carries that id through `http.request`, `patch.received`,
  `patch.loaded`, `patch.applied` / `patch.rejected`, including the version
  span and structured failure details.
- `GET /diagnostics/requests/:id` returns the persisted audit row with the full
  per-step trace; `GET /documents/:id/history` returns attempts newest-first.
- Failures and uncertainties are emitted at `warn`/`error` and kept in a
  dedicated `error` object; they are never folded into a success record.

## Checks deliberately not run

See `docs/boundary-semantics.md` §8 for the explicit list of things this suite
does **not** claim (power-loss durability, multi-process concurrency, TLS/auth,
the IETF official JSON test suite, performance). They are reported as
not-executed, not as passing.
