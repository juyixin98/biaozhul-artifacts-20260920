# Merkle State Proof Service

A versioned key-value store backed by a binary Merkle tree, with a pure JSON
HTTP API. Every write batch produces a new **immutable, content-committed
root**; anyone holding only that root can independently verify:

* **existence proofs** — a key is present at a version, with its exact value;
* **non-existence proofs** — a key is absent, proven by the *adjacent*
  predecessor/successor leaves (plus explicit boundary cases), never by an
  empty value.

Pure backend: Rust + [Axum](https://github.com/tokio-rs/axum) +
[RocksDB](https://github.com/rust-rocksdb/rust-rocksdb), SHA-256 via `sha2`.
All hashing is real cryptographic computation; there are no mocked or stubbed
operations.

---

## 1. Cryptographic specification

### 1.1 Node encodings (domain separation)

Every hash is SHA-256 over a tag-prefixed, length-framed preimage:

| node    | preimage                                                          |
|---------|-------------------------------------------------------------------|
| leaf    | `0x00 ‖ u32be(key.len) ‖ key ‖ u32be(val.len) ‖ val`              |
| inner   | `0x01 ‖ left_hash(32B) ‖ right_hash(32B)`                         |
| commit  | `0x02 ‖ u64be(version) ‖ top_hash(32B) ‖ u64be(leaf_count) ‖ u32be(height)` |

* The `0x00/0x01/0x02` tag makes leaf, inner and commit preimages mutually
  unforgeable: no leaf byte string can ever parse as an inner preimage.
* Length prefixes on leaf key/value remove concatenation ambiguity
  (`H("ab","c") != H("a","bc")`).
* The **root** a client pins is the *commit hash*, so a proof generated for
  version N cannot be replayed under version M even if the tree shape matches.
* The empty-tree top is `SHA256`'s 32 zero bytes; its root is nevertheless a
  proper tagged commit (`H(0x02 ‖ version ‖ 0^32 ‖ 0 ‖ 0)`), not zero.

### 1.2 Key ordering and tree shape

* Keys are ordered by **raw byte-wise lexicographic order** (RocksDB's
  default comparator) — binary keys are first-class.
* Level 0 is the ordered list of leaf hashes. Each parent is
  `H(0x01 ‖ left ‖ right)` over consecutive pairs.
* An odd trailing node at a level is **promoted unchanged** (no duplicate
  hashing, no zero-subtree). The proof records such a step as
  `{"side":"promoted","hash":null}`.
* `height = 0` for 0 or 1 leaf; otherwise the number of pairing rounds.

### 1.3 Existence proof

```jsonc
{
  "kind": "existence",
  "version": 1, "root": "<commit hex>", "top": "<tree-top hex>",
  "leaf_count": 5, "height": 3, "query_key_hex": "626f62",
  "index": 1, "key_hex": "626f62", "value_hex": "323031",
  "path": [
    {"side": "left",     "hash": "<32B sibling hex>"},
    {"side": "right",    "hash": "..."},
    {"side": "promoted", "hash": null}
  ]
}
```

A verifier recomputes the leaf hash from `(key_hex, value_hex)`, folds the
path bottom-up (`left` ⇒ sibling is the left child; `right` ⇒ right child;
`promoted` ⇒ carry the node up unchanged), and requires:

1. every step's position parity matches the tree geometry derived from
   `leaf_count` (a forged `promoted`/`left`/`right` is rejected);
2. the direction bits reconstruct exactly the claimed `index`;
3. the rebuilt top equals `top`, and
   `H(0x02 ‖ version ‖ top ‖ n ‖ height)` equals the **client-pinned root**;
4. the branch key equals the key the client actually asked about.

An empty value (`"value_hex": ""`) is a perfectly valid *present* value.

### 1.4 Non-existence proof (adjacency-bound, no empty-value sentinel)

Absence is never expressed as a null/empty value. Instead the prover returns
Merkle branches for the closest keys around the query:

```jsonc
{ "kind": "non_existence", ...,
  "prev": { /* full branch of greatest key < query */ } | null,
  "next": { /* full branch of smallest key > query */ } | null }
```

The independent verifier enforces, in addition to replaying both branches to
the same root:

* **interior gap:** `prev.key < query < next.key` **and**
  `next.index == prev.index + 1` — no index may be skipped between them;
* **before first:** `prev = null` and `next.index == 0`;
* **after last:** `next = null` and `prev.index == leaf_count - 1`;
* **empty tree:** both null, `leaf_count == 0`, top is the 32-zero hash.

The index is cryptographically reconstructed from the path direction bits, so
a prover cannot relabel a branch. These rules make the proof binding: if the
queried key were really present at some index `j`, two consecutive indices
could not strictly straddle it, and the boundary index checks forbid hiding a
smaller/larger neighbour.

### 1.5 Batch semantics

* A batch is an ordered list of ops. If a key repeats, **the last op in input
  order wins**.
* `Put(value)` (including `Put(empty bytes)`) and `Delete` are distinct ops;
  presence with an empty byte-string value is distinct from deletion.
* On success the batch atomically publishes version
  `current+1` with a new immutable root. Historical versions stay queryable
  forever.
* Publication failure (storage error) discards the whole RocksDB write batch:
  the version pointer never advances and no half-built root is observable.
  In-memory state is updated only after the durable `WriteBatch` commit
  (`use_fsync = true`).
* On restart, live state is rebuilt from the latest version's persisted
  manifest; committed roots survive, uncommitted work never appears.

---

## 2. HTTP API

Binary keys/values travel as hex (`*_hex`). UTF-8 convenience fields `key` /
`value` are accepted too; an explicit `*_hex` always wins.

| Method | Path                  | Description |
|--------|-----------------------|-------------|
| GET    | `/healthz`            | liveness |
| GET    | `/v1/root`            | current version root + metadata (404 before first commit) |
| GET    | `/v1/root/{version}`  | historical immutable root (404 if unknown) |
| GET    | `/v1/roots`           | all published versions, ascending |
| POST   | `/v1/batches`         | atomically apply a write batch → new root |
| POST   | `/v1/proofs`           | existence / non-existence proof at `version` (default current) |
| GET/POST | `/v1/nodes/{hash}`  | node preimage for auditing |

### `POST /v1/batches`

```json
{ "writes": [
  {"key": "alice", "value": "100"},
  {"key_hex": "626f62", "value_hex": "323031"},
  {"key": "dave"},
  {"key": "carol", "delete": true}
]}
```

A write with neither `value` nor `value_hex` is a Put of the **empty byte
string** (present-empty). `"delete": true` removes the key.

Response:

```json
{"version":1,"root_hex":"…","leaf_count":3,"height":2,
 "applied_puts":3,"applied_deletes":1}
```

### `POST /v1/proofs`

```json
{"key_hex": "627a", "version": 1}
```

Returns the existence or non-existence document from §1.3/§1.4.

Errors: `400` malformed input, `404` unknown version / no commits yet,
`500` storage failure.

---

## 3. Local startup

Requirements: Rust ≥ 1.87 (stable; tested on 1.98), a C++ toolchain,
`cmake`, `libclang` (RocksDB's bindgen needs them).

```bash
cargo build --release

# defaults: --db data/merkle-rocksdb --listen 127.0.0.1:8080
./target/release/merkle-proof-service --db data/db1 --listen 127.0.0.1:8080
# env overrides: MERKLE_DB_PATH, MERKLE_LISTEN ; log level: RUST_LOG=debug
```

### Acceptance commands

```bash
# 1) full automated test suite (unit + RocksDB integration + real HTTP e2e)
cargo test

# 2) one-command end-to-end acceptance: starts the server, applies the
#    example batches, verifies every proof with BOTH independent verifiers,
#    performs tamper tests, and checks recovery across a restart
./scripts/demo.sh
```

Manual smoke test:

```bash
curl -s localhost:8080/healthz
curl -s -XPOST localhost:8080/v1/batches -H 'content-type: application/json' \
     --data @examples/input/batch1.json
curl -s -XPOST localhost:8080/v1/proofs -H 'content-type: application/json' \
     -d '{"key":"bob"}'
```

---

## 4. Independent verifiers (trust only the root)

The verification logic exists in **three independent re-implementations**;
none shares service-side tree-building code.

1. **Rust library verifier** — `src/verifier.rs` (used by the test suite). It
   imports only SHA-256 and JSON parsing.
2. **Standalone Rust CLI** — `examples/verify.rs`, which does not even use
   the crate's `verifier` module:

   ```bash
   cargo run --release --example verify -- proof.json <root_hex> <query_key_hex>
   # or pipe on stdin: cat proof.json | cargo run --release --example verify -- - <root> <keyhex>
   # exit 0 verified, 1 rejected (precise reason on stderr)
   ```
3. **Python verifier** — `scripts/verify.py`, standard library only
   (`hashlib` + `json`), the exact same spec:

   ```bash
   python3 scripts/verify.py proof.json <root_hex> <query_key_hex>
   ```

---

## 5. Repository layout

```
Cargo.toml              locked, reproducible dependency set (Cargo.lock)
src/
  main.rs               binary entrypoint (flags, logging)
  lib.rs
  core.rs               leaf/inner/commit hashing, tree build, path generation
  store.rs              RocksDB versioned state; atomic commit; restart recovery
  proof.rs              proof document generation
  verifier.rs           independent verifier (root + proof bytes only)
  api.rs                Axum JSON API
  error.rs              typed 400/404/500 errors
examples/
  verify.rs             standalone Rust verifier CLI
  input/batch1.json     example batch: dup keys, empty value, hex value
  input/batch2.json     example batch: delete + insert
scripts/
  verify.py             independent Python verifier (stdlib only)
  demo.sh               end-to-end acceptance + cross-verifier checks
tests/
  core_tests.rs         Merkle/store/verifier tests (16 cases)
  http_tests.rs         real-server HTTP e2e tests
```

### Test coverage

* empty tree (zero-top tagged commit, empty-tree absence);
* single leaf (empty path, both boundary absences);
* tree sizes 1–8 (all odd-node promotion shapes);
* duplicate keys — last write in input order wins;
* present-empty value vs delete (distinct, different roots);
* version history immutability and historical queries;
* binary keys/values and byte ordering;
* tampered sibling hash, tampered leaf value, wrong trusted root, wrong query
  key, forged `promoted` step, non-adjacent-neighbour absence forgery;
* failed publish leaves no version/root behind;
* process restart rebuilds state and all roots from the manifest, and the
  service continues versioning correctly;
* every e2e proof is checked through the independent Rust verifier, and the
  demo additionally cross-checks with the Python verifier.
