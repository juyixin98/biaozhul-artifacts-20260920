# ecstripe — Erasure-Coded Stripe Recovery (Rust)

A pure-backend, streaming **systematic Reed-Solomon erasure-coding** library
plus a JSON-driven command-line entry point. It splits a blob into `k` data
shards per stripe, generates `m` parity shards, and recovers known lost shards.

- **Field arithmetic**: built on the mature [`gf256`](https://crates.io/crates/gf256)
  crate — GF(2⁸) with irreducible polynomial `0x11d` (x⁸+x⁴+x³+x²+1) and
  primitive element `0x02`.
- **Implemented from scratch in this crate**: the systematic coding matrix
  (identity + Cauchy parity block), Gauss–Jordan inversion over GF(2⁸),
  stripe parity generation and shard recovery.
- **Streaming**: one stripe is buffered at a time, so peak working memory is
  bounded by `(k + m) * stripe_size`, independent of total input length.
- **Bounded**: explicit `max_memory` and `max_output` budgets are enforced by
  both the library and the JSON entry point.
- **Documented format**: the binary container is specified below.
- No frontend, no network, no unsafe code.

## Recovery guarantee — read this

Recovery is promised **only for known erasures when the number of missing
shards does not exceed `m`** (any `k` of `n = k + m` shards suffice).
Specifically:

| Situation | Result |
|---|---|
| `<= m` known-missing shards | Recovered exactly |
| `> m` shards missing | Error `not_enough_shards` — recovery is impossible |
| Present shard with a length inconsistent with the header | Error `length_mismatch` |
| Truncated/framed-damaged container | Error `bad_record` / `unexpected_eos` / `bad_magic` |
| Present but **silently corrupted** shard | **Not detectable by Reed-Solomon** — decoding yields wrong-but-consistent bytes. Verify with the optional external SHA-256 (`expect_hash`) |

Reed-Solomon here is an *erasure* code, not an error-correcting decoder: it
cannot tell a corrupted shard from a genuine one. Integrity is established
externally — the encoder can record a SHA-256 of the plaintext in the header,
and the decoder verifies it (`"tag_hash": true` / `"expect_hash": true`).

## Building

```bash
cargo build --release
cargo test
```

The binary is `target/release/ecstripe`.

## JSON control entry point

The CLI reads **one JSON request document** (a file path, or `-` for stdin)
and prints **one JSON response document** to stdout:

```bash
ecstripe <encode|decode|info|corrupt> request.json
```

Success:

```json
{ "status": "ok", "data": { ... } }
```

Failure (exit code 1):

```json
{ "status": "error", "error": { "kind": "not_enough_shards", "message": "..." } }
```

### `encode`

```json
{
  "input": "input.bin",
  "output": "data.enc",
  "data_shards": 3,
  "parity_shards": 2,
  "stripe_size": 4096,
  "total_len": 10000,
  "tag_hash": true,
  "limits": { "max_memory": 268435456, "max_output": 1073741824 }
}
```

`total_len` is optional (defaults to the input file length). `tag_hash`
records the plaintext SHA-256 in the header. `limits` are optional.

Response: `{"output", "stripes", "bytes", "sha256"}`.

### `decode`

```json
{
  "input": "data.enc",
  "output": "decoded.bin",
  "loss": { "shards": [1, 4], "cells": [[0, 0], [2, 3]] },
  "expect_hash": true,
  "limits": { "max_memory": 268435456, "max_output": 1073741824 }
}
```

`loss` simulates known erasures (and models shards being unavailable):
`shards` lists shard ids lost on **every** stripe; `cells` lists specific
`[stripe, shard]` pairs. Both are optional. `expect_hash` verifies the
decoded plaintext against the header SHA-256.

Response: `{"output", "stripes", "bytes", "sha256"}`.

### `info`

```json
{ "input": "data.enc" }
```

Prints the validated header JSON (`data_shards`, `parity_shards`,
`stripe_size`, `total_len`, `stripe`-derived fields, optional `sha256`).

### `corrupt` (testing helper)

Copies a container while XOR-flipping selected payload bytes, leaving framing
intact — used to demonstrate that silent corruption needs an external check:

```json
{
  "input": "data.enc",
  "output": "data.corrupt.enc",
  "flips": [{ "stripe": 0, "shard": 0, "offset": 0, "xor": 255 }]
}
```

`xor` is optional (default `255`). Fails if a flip offset does not land inside
the named shard.

## Container format (v1)

All multi-byte integers are big-endian.

```text
+------------------+---------------------------------------------+
| magic            | 4 bytes: ASCII "ECS1"                       |
+------------------+---------------------------------------------+
| header_len       | u32: length of header_json in bytes         |
| header_json      | header_len bytes, UTF-8 JSON                |
+------------------+---------------------------------------------+
| records ...      | zero or more framed records                 |
+------------------+---------------------------------------------+
| end-of-stream    | exactly one EOS record, always last         |
+------------------+---------------------------------------------+
```

Header JSON:

```json
{
  "format": "ecstripe",
  "version": 1,
  "data_shards": 3,
  "parity_shards": 2,
  "stripe_size": 4096,
  "total_len": 10000,
  "sha256": "9f2f…64 lowercase hex chars (optional)"
}
```

Constraints: `1 <= data_shards`, `1 <= parity_shards`,
`data_shards + parity_shards <= 256`, `stripe_size >= 1`.

### Records

- **chunk** (`0x01`): `tag:u8`, `stripe:u32`, `shard:u8`, `chunk_len:u32`,
  `payload:[u8; chunk_len]`. `chunk_len <= 2^20`; larger shards are split
  across records by the encoder. A zero-length chunk explicitly marks a
  zero-length shard as present.
- **end-of-stream** (`0x02`): `tag:u8`, `stripe_count:u32`.

### Stripe and shard layout

A stripe packs `k` data shards of `S = stripe_size` bytes, i.e. `k * S`
original bytes. The number of stripes is `T = ceil(total_len / (k * S))`.
Data shard `j` of stripe `t` covers original bytes `(t * k + j) * S ..`; its
on-wire length is `min(S, total_len - offset)` (zero-length shards occur only
on the final stripe). Coding is performed on data zero-padded up to `S`;
parity shards are always emitted full-length (`S`). The decoder strips
padding using `total_len`.

Records of a `(stripe, shard)` must appear in order, non-decreasing shard id
within a stripe; stripes must be consecutive. Duplicate/overlapping shards,
oversized or out-of-range records, and length inconsistencies are rejected.

### Coding matrix

With `D = [d_0 … d_{k-1}]^T` the data-shard column vectors, parity is
`P = C_tail · D` where the `n × k` systematic coding matrix `C` is:

```text
C = [ I_k ]                 (rows 0..k-1: data shards select themselves)
    [ M   ]                 (rows k..k+m-1: Cauchy parity block)
```

Parity row `p`, column `j` is the Cauchy entry
`M[p][j] = 1 / (x_p + y_j)` over GF(2⁸), with `y_j = j`
(`0..k-1`) and `x_p = k + p` (`k..k+m-1`); field addition is XOR. The two
point sets are disjoint, so no denominator is zero. Every square submatrix of
a Cauchy matrix is nonsingular, hence `C` is MDS and **any** selection of `k`
surviving rows is invertible. (A naive Vandermonde parity block lacks this
guarantee on GF(2⁸): submatrices with non-consecutive exponent sets can be
singular.) The decoder selects the surviving rows, inverts that `k × k`
matrix with Gauss–Jordan elimination (partial pivoting and field
reciprocals) and reconstructs all missing shards.

## Library API

```rust
use ecstripe::codec::{encode_stream, decode_stream, Limits, LossPattern};

// W: io::Write + io::Seek (the encoder patches the fixed-size hash field)
encode_stream(&mut input, &mut output, k, m, stripe_size, total_len, tag_hash, limits)?;
decode_stream(&mut input, &mut output, &loss, expect_hash, limits)?;
```

Lower-level per-stripe functions are available in `ecstripe::coding`:

```rust
let parity = ecstripe::encode_stripe(&[&d0[..], &d1[..], &d2[..]], m)?;
let recovered = ecstripe::recover_stripe(k, m, stripe_index, stripe_len, &present)?;
```

## Project layout

```text
Cargo.toml
src/
  lib.rs        public API
  error.rs      typed error enum (all failure modes)
  coding.rs     coding matrix, Gauss–Jordan inversion, stripe encode/recover
  container.rs  documented v1 binary format + Header
  codec.rs      streaming encoder/decoder, limits, framing validation
  cli.rs        JSON request/response control entry point
  main.rs       thin process wrapper
tests/
  integration.rs  acceptance tests (enumerated loss, over-loss, length
                  mismatch, silent corruption + external hash, limits)
requests/        example JSON requests
```

## What is intentionally out of scope

- Reconstruction when more than `m` shards are missing (information-theoretically impossible).
- Detection of silent corruption without an external integrity value.
- A frontend or network service.
