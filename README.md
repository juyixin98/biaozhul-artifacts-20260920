# rbitset — 游程位图运算（streaming hybrid bitmap-set library）

A pure-Rust, **zero-dependency** backend library and CLI for sets of
**32-bit unsigned integers**, stored as a hybrid of sorted sparse arrays and
bitmaps (a Roaring-style design, independently implemented — no roaring
crate). It provides:

* hybrid `array` / `bitmap` containers with automatic, semantics-preserving
  switching at the 4 096-value crossover;
* set algebra — **union, intersection, difference**;
* a **streaming binary codec** over any `std::io::{Read,Write}`, with an
  explicit, documented wire format ([`docs/format.md`](docs/format.md));
* bounded decoding: explicit **input-byte** and **decoded-value** budgets
  plus structural caps, so forged lengths cannot force large allocations or
  over-reads;
* a **JSON control entry point** (strict hand-rolled JSON + base64) usable as
  a CLI or as a library function. No frontend.

## Layout

```
src/
  error.rs       typed errors
  container.rs   Array / Bitmap containers + per-container set algebra
  set.rs         IntSet: high-16-bit chunk map + union/intersection/difference
  codec.rs       streaming RBST v1 encoder/decoder with byte/value budgets
  json.rs        strict JSON parser/serialiser, base64, request dispatch
  main.rs        CLI front-end (one JSON request -> one JSON response)
  lib.rs         library root
tests/
  acceptance.rs  random BTreeSet comparison, boundaries, dense/sparse, hostile
  json_api.rs    end-to-end JSON control-surface tests
docs/format.md   the wire-format specification
examples/        sample JSON requests
```

## Build and test

Requires a stable Rust toolchain (edition 2021, no external crates).

```bash
cargo build --release
cargo test
cargo run --release -- examples/encode.json
```

## CLI

The binary reads one JSON request and prints one JSON response.

```bash
# from a file
rbitset examples/encode.json

# from stdin
echo '{"op":"encode","set":{"values":[1,2,3]}}' | rbitset -

# inline
rbitset '{"op":"contains","set":{"values":[1,42]},"value":42}'
```

Exit code is `0` when a well-formed response with `"ok":true` is produced,
`1` for an `"ok":false` business result (bad request, malformed stream,
breached limit) so shell pipelines can detect failure, and `2` for usage or
unreadable-input errors.

## Request format

Common fields:

| field        | type     | meaning                                            |
|--------------|----------|----------------------------------------------------|
| `op`         | string   | one of `encode` `decode` `info` `union` `intersection` `difference` `contains` |
| `max_bytes`  | integer  | decode byte budget (default 64 MiB)                |
| `max_values` | integer  | decode value budget (default 2^32)                 |

A set operand is either `{"values":[u32,...]}` or
`{"data":"<base64 RBST>"}`.

### `encode`

```json
{ "op": "encode", "set": { "values": [1, 2, 65536, 4294967295] } }
```

Response: `{"ok":true,"data":"<base64>","bytes":N,"cardinality":K,"chunks":C}`.

### `decode` / `info`

```json
{ "op": "decode", "data": "<base64 RBST>", "max_bytes": 16777216, "max_values": 1000000 }
```

`decode` returns `values` and per-chunk container summaries; `info` returns
metadata only (no `values`). See [`examples/`](examples/).

### `union` / `intersection` / `difference`

```json
{ "op": "difference", "a": {"values":[1,2,3]}, "b": {"values":[3,4]} }
```

The result is returned both as re-encoded base64 (`data`) and as `values`,
with `cardinality`. `difference` is set-relative: values in `a` not in `b`.

### `contains`

```json
{ "op": "contains", "set": {"values":[1,42]}, "value": 42 }
```

Returns `{"ok":true,"value":42,"contains":true}`.

### Errors

Any failure is a JSON object `{"ok":false,"error":"..."}` — malformed JSON,
unknown op, non-u32 value, bad base64, truncated/non-canonical binary stream,
or a breached limit. No error message leaks host paths or stack traces.

## Design notes

* **Decomposition.** A `u32` splits into a high-16-bit chunk key and a
  low-16-bit slot. Each non-empty key owns one container covering 65 536
  slots. Keys are kept in a `BTreeMap`; containers serialise in ascending key
  order.
* **Containers.** `Array` = sorted unique `u16`s (≤ 4 096); `Bitmap` = 1 024
  `u64` words (8 KiB). Operations dispatch on the `(kind, kind)` pair:
  array/array uses sorted merges, bitmap/bitmap uses word-wise bit ops, and
  mixed pairs probe/merge without a full conversion. Every result is
  re-canonicalised, so representation follows cardinality only.
* **Streaming & limits.** Encode writes incrementally; decode reads one
  chunk at a time and validates every declared length before consuming its
  payload. Array lengths > 4 096 are rejected up front; bitmap payloads are a
  fixed 8 KiB; cardinality claims are checked against the actual popcount and
  against the header total. Peak decode memory is bounded by one bitmap plus
  the result set.
* **No unsafe code**, no external crates.

## Acceptance evidence

The acceptance suite (`cargo test`) covers, deterministically (seeded LCG, no
test-time randomness):

* random sets compared element-for-element against `std::collections::BTreeSet`
  for membership, union, intersection and difference, across sparse →
  extremely dense profiles;
* the array/bitmap boundary on both sides (4 095 / 4 096 / 4 097);
* chunk-key boundaries (`0`, `0xFFFF`, `0x10000`, `0x7FFF_FFFF`,
  `0x8000_0000`, `0xFFFF_FFFF`) and all four container-kind pairings;
* extremely dense (full 65 536-value chunk) and extremely sparse data;
* hostile inputs: forged chunk counts, over-cap array lengths, bitmap
  cardinality lies, truncated payloads, and tight byte/value budgets.

See the run log in [`RUNLOG.md`](RUNLOG.md).
