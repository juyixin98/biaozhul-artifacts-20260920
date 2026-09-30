# tritmap

Column-value bitmap indexes with SQL three-valued-logic filtering
(`AND` / `OR` / `NOT` / `IS NULL`) over a versioned, deletion-aware document
universe.

Built with **Rust + Axum + arrow2**. All data is local synthetic fixture data
(`fixtures/users.csv` plus randomized generator output) — no network services
or production accounts are involved.

## What it does

A WHERE predicate does not have two outcomes in SQL: every row is `TRUE`
(kept), `FALSE` (rejected), or `UNKNOWN` (a NULL affected the comparison and
the result is neither true nor false). Deleted rows are a fourth, separate
state — they belong to **no** set because they are outside the live universe.

This project stores the three outcomes as three bitmaps over one explicit
document universe and composes them under Kleene's strong three-valued logic:

```text
AND   T F U      OR    T F U      NOT
  T   T F U        T   T T T       T -> F
  F   F F F        F   T F U       F -> T
  U   U F U        U   T U U       U -> U
```

The decisive NULL crossings are handled structurally: `UNKNOWN AND FALSE =
FALSE` (a FALSE conjunct decides), `UNKNOWN OR TRUE = TRUE`, and
`NOT UNKNOWN = UNKNOWN`.

## Boundary guarantees

The four failure modes called out in the requirements are prevented by
construction, not by convention:

1. **True / False / Unknown operate inside one document universe.** Every
   predicate produces a validated [`TritPartition`](src/partition.rs): the
   three sets are pairwise disjoint, subsets of the live-domain bitmap, and
   their union equals it. Every live row is classified exactly once
   (`TritPartition::validate`).
2. **NOT is never a machine-word complement of the TRUE bitmap.**
   `TritPartition::not` swaps the TRUE and FALSE sets and leaves UNKNOWN and
   the domain untouched. A word-wise `!true_bits` would fold FALSE and
   UNKNOWN together and re-admit deleted rows.
3. **Invalid tail bits are always zero.** [`Bitmap`](src/bitset.rs) masks the
   last 64-bit word to the exact universe size in every constructor and in
   `complement`; the universe-relative complement, not the word boundary,
   defines NOT. `Bitmap::debug_assert_valid` checks the invariant; boundary
   tests cover lengths 0, 1, 7, 8, 9, 63, 64, 65, 67, 127, 128, 129, 200 and
   1000.
4. **Versioned deletions exclude rows from the whole universe; only
   compatible index snapshots combine.** Deletions are append-only
   tombstones with a monotonic version (`src/deletion.rs`). Queries pin one
   version and evaluate on that version's live-domain bitmap, so a deleted
   row appears in none of T/F/U (not as UNKNOWN). Combining partitions over
   different domains returns `PARTITION_ERROR / DOMAIN_MISMATCH`; combining
   indexes from different snapshots returns `INCOMPATIBLE_INDEX_VERSION`.
   Historical versions remain queryable.

## Project layout

```text
src/
  bitset.rs        Fixed-length LSB-first bitmaps, tail masking, set algebra
  trit.rs          Trit enum (TRUE/FALSE/UNKNOWN) + Kleene AND/OR/NOT
  partition.rs     Validated three-set partition over an explicit domain
  batch.rs         Typed arrow2 batches (INTEGER / TEXT / BOOLEAN, NULL validity)
  index.rs         Column value -> bitmap indexes; leaf predicate evaluation
  deletion.rs      Append-only versioned tombstones; live domain per version
  query/           Expression AST, hand-written parser, tree evaluation
  dataset.rs       Resource/state: immutable snapshot + indexes + deletion log
  oracle.rs        Independent row-by-row scalar reference (no bitmap primitives)
  verify/          Differential cross-check harness + deterministic case generator
  server/          Axum router, typed error envelopes, shared AppState
  config.rs        TOML file + TRITMAP_* environment overrides, validated
  loader.rs        Local synthetic CSV loader (empty field = SQL NULL)
  main.rs          CLI: serve | verify | query
tests/             Independent integration tests (fixed assertions + differential + HTTP)
fixtures/          users.csv synthetic table
config/            tritmap.toml example configuration
```

The layers follow the requested organization: typed batches → query operators
→ resources/state → verification entry, with an independent test layer and a
separate configuration layer.

## Build

Requires Rust 1.98.1 (the toolchain version is declared in `Cargo.toml`). Key
dependencies are pinned with `=` and the full dependency graph is recorded in
`Cargo.lock`; build with `--locked` for a reproducible dependency set:

```bash
cargo build --locked --release
```

## Run the tests

```bash
cargo test --locked            # 60 tests: 35 unit + 25 integration
cargo clippy --locked --all-targets
cargo fmt --check
```

The independent test files assert concrete results, not callability:

- `tests/threeval_logic_test.rs` — hand-computed T/F/U rows for NULL-crossing
  AND/OR/NOT/IS NULL on a fixed 10-row fixture.
- `tests/bitmap_boundaries_test.rs` — tail bytes at awkward universe lengths,
  NOT-vs-wordwise-negation, deletion exclusion at current/historical
  versions, incompatible-domain rejection, all-UNKNOWN partition, mixed
  NULL+deletion oracle agreement.
- `tests/differential_test.rs` — randomized synthetic cases (xorshift64,
  seeded and reproducible) at every boundary length plus 60 varied cases,
  cross-checked row by row against the scalar oracle; asserts the concrete
  failure category when something disagrees.
- `tests/http_api_test.rs` — real Axum server on an ephemeral port; asserts
  response bodies, three-valued counts and HTTP error categories.

### Why the reference answers are trustworthy

`src/oracle.rs` is written on deliberately different primitives: plain
`Vec<Option<Scalar>>` from the arrow batch, per-row scalar Kleene evaluation,
and an explicit hash-set of dead row ids (deleted → `None`, never `Unknown`).
The bitmap engine cannot influence that code path, so agreement across
thousands of generated rows is a genuine differential test. Test expectations
are either hand-transcribed literals or oracle outputs — never values
produced by the engine under test.

## CLI usage

All CLI output on stdout is machine-readable JSON; logs go to stderr.

Evaluate one predicate against the fixture table (the result is also
automatically cross-checked against the scalar oracle):

```bash
$ cargo run --locked -- query --expr "age >= 18 AND city IS NOT NULL"
{"ok":true,"expr":"age >= 18 AND city IS NOT NULL","version":1,"row_space":19,
 "live_rows":19,"true_rows":6,"false_rows":7,"unknown_rows":6,"deleted_rows":0,
 "selected_rows":[4,5,8,9,13,16],"rows":[...]}
```

Delete rows at successive versions (the deletion is immediately visible;
`--version` pins the query version, and the oracle only treats tombstones at
or below that version as dead):

```bash
$ cargo run --locked -- query --expr "id IS NOT NULL" --delete 0
... "version":2,"live_rows":18,"deleted_rows":1 ...
$ cargo run --locked -- query --expr "id IS NOT NULL" --delete 0 --version 1
... "version":1,"live_rows":19,"deleted_rows":0 ...   # historical view
```

Run the built-in differential verification (non-zero exit on any mismatch):

```bash
cargo run --locked -- verify --iterations 24 --rows 200 --null-density 40
cargo run --locked -- verify --seed 1234 --iterations 10 --rows 67 --null-density 100
```

The first `BOUNDARY_LENGTHS` cases always use the awkward universe lengths
(1, 2, 7, 8, 9, 63, 64, 65, 67, 127, 128, 129, 200); the following case is an
all-NULL (all-UNKNOWN) case; the rest use the requested size and density.

Start the HTTP service:

```bash
cargo run --locked -- serve                    # defaults: 127.0.0.1:8080
TRITMAP_PORT=18099 cargo run --locked -- serve # override
```

## HTTP API

Every response is an envelope: `{"ok": true, "data": ...}` or
`{"ok": false, "error_code": "...", "error": "..."}`. Exceptional states
never return a success envelope. Each request is logged with a correlation
`run_id` (plus table, expression and pinned version).

```bash
curl -s localhost:8080/health
curl -s localhost:8080/tables
curl -s localhost:8080/tables/users

curl -s -X POST localhost:8080/tables/users/query \
  -H 'Content-Type: application/json' \
  -d '{"expr":"NOT (age >= 18 AND city = '\''NY'\'') OR vip IS NULL","version":1}'

curl -s -X POST localhost:8080/tables/users/rows/2/delete \
  -H 'Content-Type: application/json' -d '{"version":2}'

curl -s -X POST "localhost:8080/verify?iterations=15&rows=67&null_density=40"
```

The query response contains aggregate counts (`true_rows`, `false_rows`,
`unknown_rows`, `deleted_rows`) whose sum is the row-space size, per-row
TRUE/FALSE/UNKNOWN verdicts for live rows, and the selected row ids. Error
categories include `QUERY_PARSE_ERROR` (400), `TYPE_MISMATCH` (400),
`UNKNOWN_TABLE`/`UNKNOWN_COLUMN`/`ROW_NOT_FOUND` (404), `ROW_ALREADY_DELETED`
/`VERSION_OUT_OF_ORDER`/`INCOMPATIBLE_INDEX_VERSION` (409),
`PARTITION_ERROR`, and `BITMAP_ERROR`.

Query grammar:

```text
or    := and (OR and)*
and   := unary (AND unary)*
unary := NOT unary | atom
atom  := '(' or ')' | column IS [NOT] NULL | column <op> literal
op    := = | <> | != | < | <= | > | >=
literal := integer (sign allowed) | 'text' ('' escapes ') | TRUE | FALSE
```

## Configuration

`config/tritmap.toml` documents every setting; `cargo run -- --config
config/tritmap.toml` selects it. Without a file, built-in defaults apply,
then `TRITMAP_*` environment variables override:

| Env var | Setting |
|---|---|
| `TRITMAP_HOST`, `TRITMAP_PORT` | bind address |
| `TRITMAP_FIXTURE_DIR`, `TRITMAP_TABLE_FILE` | local CSV fixture path |
| `TRITMAP_DATA_VERSION` | snapshot version of the loaded data (>= 1) |
| `TRITMAP_VERIFY_SEED`, `TRITMAP_VERIFY_ITERATIONS` | verification defaults |

Bad configuration fails startup with `CONFIG_ERROR` rather than silently
falling back. `TRITMAP_LOG` controls log filtering (e.g. `TRITMAP_LOG=debug`).

## Verification logging

Verification logs tie every judgment to an input/run identity: the `run_id`,
case name, regenerating seed, pinned data and query versions, row-space and
live counts, the expression AST, per-256-row progress, aggregate engine-vs-
oracle counts, and a concrete `basis` sentence per discrepancy. Discrepancies
are classified into stable categories rather than a boolean:

- `DELETED_ROW_CLASSIFIED` — a tombstoned row leaked into T/F/U,
- `LIVE_ROW_UNCLASSIFIED` — a live row is absent from T/F/U,
- `TRUTH_MISMATCH` — engine trit differs from the scalar Kleene result,
- `ENGINE_PARTITION_INVALID` — disjoint/exhaustive self-validation failed,
- `COUNT_MISMATCH` — aggregate counts disagree.

Any failing generated case can be regenerated exactly from its logged seed
(`generate_case(name, seed, rows, null_density)`).

## Limitations

- **Types**: only `INTEGER` (Int64), `TEXT` (Utf8) and `BOOLEAN` are indexed.
  Floats are deliberately excluded from value indexes — equality on floats is
  semantically dubious; other arrow types are rejected with
  `UNSUPPORTED_TYPE`.
- **Storage**: single in-memory, immutable snapshot per table. Deletions are
  tombstones in memory; nothing is persisted and there is no append/update of
  rows.
- **Query language**: the predicate grammar covers boolean filters only
  (comparisons, IS [NOT] NULL, AND/OR/NOT). There are no projections, joins,
  aggregation, arithmetic or ORDER BY/LIMIT.
- **Concurrency**: the catalog uses a process-local `RwLock`; there is no
  cross-process coordination or authentication. Intended for local use with
  synthetic data only.
- **Scale**: indexes hold a bitmap per distinct value in a `BTreeMap` —
  appropriate for the synthetic/fixture scale, not a production compression
  or concurrency strategy.
