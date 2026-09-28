# RFC 6902 Restricted JSON Document Update Service

A multi-module backend that applies a **restricted subset of RFC 6902** JSON
Patches — `test`, `add`, `remove`, `replace`, `move`, `copy` — to versioned JSON
documents. Patches are **atomic**, **bound to an expected document version**,
and produce **request-correlated, per-step diagnostics**.

Everything runs locally with synthetic data; no external accounts or real
business data are required.

- Stack: TypeScript · Node.js (>=22) · Fastify 4 · SQLite (better-sqlite3) · Vitest
- All dependency versions are **pinned exactly** (no `^`/`~`).

## What is guaranteed

1. **JSON Pointer parsing per RFC 6901** — `/`-separated tokens, empty tokens
   (empty object keys) preserved, `~1`→`/` and `~0`→`~` unescaped in that
   order, illegal escapes rejected.
2. **Array semantics** — digit-only indices, no leading zeros (except `0`),
   `-` means append, insertion shifts right, removal shifts left.
3. **Sequential evaluation** — each operation takes the previous operation's
   result; operations are parsed lazily at their execution position.
4. **Atomicity** — any failure (contract, pointer, or a failing `test`) aborts
   the whole patch; the stored document and its version are unchanged. The
   kernel never mutates its input.
5. **`move` safety** — moving a value into one of its own proper descendants is
   rejected (`MOVE_INTO_DESCENDANT`).
6. **Version binding** — a patch may declare `expectedVersion`; a mismatch is
   rejected before any operation runs (`VERSION_CONFLICT`).

## Modules

| Module | Responsibility |
| --- | --- |
| `src/pointer.ts` | JSON Pointer parse/escape, array index rules, typed resolution |
| `src/equality.ts` | RFC 8259 deep equality used by `test` |
| `src/errors.ts` | Stable failure categories + HTTP status mapping |
| `src/patch.ts` | Contract parsing + the pure, immutable execution kernel and per-step traces |
| `src/store.ts` | SQLite state adapter: documents, versions, immediate transactions, audit table |
| `src/service.ts` | Orchestration: version check → kernel → CAS commit/rollback → audit |
| `src/logger.ts` | Request-correlated JSONL diagnostic logger |
| `src/server.ts` | Fastify diagnostic interface and response envelope |
| `src/config.ts` | Env configuration, validated at startup (fail fast) |
| `src/index.ts` | Process entry point |

The kernel contains no SQL; the store contains no patch logic.

## Quick start

```bash
npm install        # exact pinned versions
npm run start      # http://127.0.0.1:8080, ./data/service.sqlite, ./logs/service.log
```

Configuration (all optional, local defaults):

| Variable | Default | Meaning |
| --- | --- | --- |
| `PORT` | `8080` | HTTP port (1..65535) |
| `HOST` | `127.0.0.1` | Bind address |
| `DATABASE_PATH` | `./data/service.sqlite` | SQLite file (`:memory:` supported) |
| `LOG_FILE` | `./logs/service.log` | JSONL diagnostic log |
| `LOG_STDOUT` | `0` | Also echo logs to stdout |
| `PRETTY_STORAGE` | `0` | Pretty-print stored JSON |

## API

All responses use one envelope:

```json
{ "success": true, "data": { }, "error": null, "meta": { "requestId": "..." } }
```

| Method & path | Purpose |
| --- | --- |
| `POST /documents` | Create `{ "id": "...", "doc": <json> }` at version 0 |
| `GET /documents` | List ids and versions |
| `GET /documents/:id` | Read document + version |
| `POST /documents/:id/patch` | Apply `{ "expectedVersion": n?, "patch": [ ... ] }` |
| `GET /diagnostics/requests/:requestId` | Full audit record incl. per-step traces |
| `GET /documents/:id/history?limit=20` | Recent audit records, newest first |
| `GET /health` | Liveness |

Supply a correlation id with the `X-Request-Id` header (or `requestId` in the
patch body); otherwise one is generated.

### Example

```bash
curl -s -X POST localhost:8080/documents -H 'content-type: application/json' \
  -d '{"id":"d1","doc":{"tags":["a","b"]}}'

curl -s -X POST localhost:8080/documents/d1/patch -H 'content-type: application/json' \
  -H 'x-request-id: demo-1' \
  -d '{"expectedVersion":0,"patch":[
        {"op":"test","path":"/tags/0","value":"a"},
        {"op":"add","path":"/tags/-","value":"c"}]}'
```

A failing patch returns the failure category and per-step traces, HTTP status
reflects the category, and the document version is unchanged.

## Tests and verification

```bash
npm test           # unit + integration + differential + HTTP (Vitest)
npm run coverage   # same, with v8 coverage (80% gate enforced)
npm run verify     # full pipeline incl. booting the real server + HTTP scenario
```

- `test/pointer.test.ts`, `test/kernel.test.ts` — concrete edge assertions
  (shifts, empty/escaped keys, mid-patch rollback, descendant move, …).
- `test/service.test.ts` — SQLite transactions, version conflicts, rollback,
  audit trail, concurrent writers.
- `test/http.test.ts` — full HTTP surface via Fastify in-process injection.
- `test/differential.test.ts` — **1000 seeded cases + fixtures** checked
  against an **independent reference implementation**
  (`test/reference/naiveReference.ts`) written in a deliberately different
  style (deep-clone + in-place mutation vs. immutable rebuild). The reference
  is not generated from the kernel.
- `fixtures/` — reusable synthetic documents and patch files.
- `scripts/e2e-scenario.mjs` — asserts concrete results over real HTTP.

## Further reading

- [`docs/boundary-semantics.md`](docs/boundary-semantics.md) — exact edge
  semantics, failure category table, non-goals, and checks not executed.
- [`docs/verification.md`](docs/verification.md) — how to reproduce the
  acceptance evidence and how to read it.
