# RUNLOG — actual build and test record

Date: 2026-09-25. Host: Linux 6.8.0-90-generic (x86_64). All commands below
were actually executed; results are copied from real output.

## 0. Toolchain note (environment problem, not project code)

The preinstalled toolchain under `~/.rustup` was incomplete (only `cargo`,
no `rustc`; `rustup component add rustc` failed with "missing manifest").
Reinstalling into the default `RUSTUP_HOME` repeatedly failed because several
other, unrelated sessions on the shared machine were concurrently running
`rustup toolchain install stable`, whose processes renamed each other's
download cache files (`could not rename ... .partial ... No such file or
directory`) and periodically panicked in rustup's disk I/O task.

Resolution used for this project: a **complete stable toolchain that another
session had finished installing** was used directly by PATH, without touching
the contended default home:

```bash
export RUSTUP_HOME=$HOME/rustup-rb
export PATH="$HOME/rustup-rb/toolchains/stable-x86_64-unknown-linux-gnu/bin:$PATH"
```

Versions actually used:

```text
rustc 1.98.1 (48a229cea 2026-09-01)
cargo 1.98.1 (797e8a9bc 2026-08-05)
```

The project itself declares edition 2021 and **no external dependencies**, so
any recent stable Rust builds it.

## 1. Build

```text
$ cargo build --release
   Compiling rbitset v0.1.0
    Finished `release profile [optimized] target(s)
```

Initial build surfaced three real source errors (let-chains used under
edition 2021, which requires edition 2024). All three were rewritten to
edition-2021-compatible code; the build then succeeded with zero warnings.

## 2. Formatting and lint

```text
$ cargo fmt --check        # clean (after one `cargo fmt` pass)
$ cargo clippy --all-targets -- -D warnings
    Checking rbitset v0.1.0
    Finished `dev profile [unoptimized + debuginfo] target(s)
```

Clippy initially reported three style lints (`map_or` → `is_none_or`,
`% 4 != 0` → `!is_multiple_of(4)`, and one dead-code method). All fixed;
clippy now passes with warnings treated as errors.

## 3. Tests

```text
$ cargo test --release
running 21 tests  ...  test result: ok. 21 passed; 0 failed   (src unit tests)
running 0  tests  ...  test result: ok.  0 passed; 0 failed   (binary)
running 16 tests  ...  test result: ok. 16 passed; 0 failed   (tests/acceptance.rs)
running 7  tests  ...  test result: ok.  7 passed; 0 failed   (tests/json_api.rs)
running 0  tests  ...  test result: ok.  0 passed; 0 failed   (doc tests)
```

Total: **44 passed, 0 failed.**

### Bugs found by the tests during development (all in test expectations,
not in the implementation) and fixed:

1. `set::tests::chunk_boundaries_preserved` asserted 2 chunks for a set whose
   high keys are `0x0000, 0x0001, 0xffff` — the correct count is 3. The
   implementation was right; the assertion was corrected.
2. `extremely_sparse_handful_of_values_across_space` compared output against
   the *unsorted* input. The set contract is ascending output; the expected
   vector was corrected to sorted order.
3. The JSON test helper treated a parse failure as a harness panic. The real
   CLI returns an `ok:false` response for malformed JSON; the helper was made
   to match, and the `malformed_requests_report_ok_false` case now exercises
   that path.

No implementation defect survived the suite. The acceptance tests
additionally cross-check the wire format against an independent Python
encoder (see §5).

## 4. CLI end-to-end (real output)

```text
$ rbitset examples/encode.json
{"bytes":43,"cardinality":5,"chunks":3,
 "data":"UkJTVAEAAwAAAAUAAAAAAAAAAAAAAwABAAIAAwABAAABAAAA//8AAQD//w==","ok":true}

$ rbitset examples/decode.json
{"cardinality":3,"chunks":1,
 "containers":[{"cardinality":3,"key":0,"kind":"array"}],
 "ok":true,"values":[1,2,3]}

$ rbitset examples/union.json
{"bytes":42,"cardinality":7,"ok":true,"operation":"union",
 "values":[1,2,3,4,5,65535,65536], ...}

$ rbitset examples/intersection.json
... "cardinality":3 ... "values":[2,3,100]

$ rbitset examples/difference.json
... "cardinality":3 ... "values":[1,2,65536]

$ rbitset examples/contains.json            -> {"contains":true,"ok":true,"value":42}
$ rbitset '{"op":"contains",...,"value":7}' -> {"contains":false,"ok":true,...}
$ echo '{"op":"encode","set":{"values":[1,2,3]}}' | rbitset -
{"bytes":29,"cardinality":3,"chunks":1,
 "data":"UkJTVAEAAQAAAAMAAAAAAAAAAAAAAwABAAIAAwA=","ok":true}
```

Error / limit paths and exit codes:

```text
$ rbitset examples/decode_limited.json      (max_values = 2)
{"error":"declared length 3 exceeds the configured limit 2","ok":false}      exit=1

$ rbitset '{"op":"nope"}'
{"error":"bad request: unknown op \"nope\" ...","ok":false}                  exit=1

$ rbitset '{"op":"contains","set":{"values":[4294967296]},"value":1}'
{"error":"bad request: set.values[] must be a non-negative integer ..."}     exit=1

# valid base64, RBST payload truncated by one value:
{"error":"unexpected end of input: need 29 bytes, only 27 available"}        exit=1

# valid base64, wrong magic:
{"error":"bad magic or unsupported version"}                                 exit=1

$ rbitset a b        -> usage message, exit=2
$ rbitset /missing   -> "failed to read ...", exit=2
```

## 5. Independent format cross-check

The documented encoding of `{1,2,3}` produced by an independent Python
implementation (header + array chunk) is
`UkJTVAEAAQAAAAMAAAAAAAAAAAAAAwABAAIAAwA=`, which is **byte-for-byte** what
the Rust CLI emits (§4). Documented byte sizes were also verified through the
CLI:

| Set | Documented | Measured |
|-----|-----------:|---------:|
| empty | 18 | 18 |
| `{1,2,3}` one array chunk | 29 | 29 |
| one full chunk (65536 values), bitmap | 8217 | 8217 |

## 6. Coverage

A line-coverage percentage could **not** be produced in this environment:
the `llvm-tools-preview` component needed by `cargo llvm-cov` is absent from
the minimal toolchain, and `rustup component add llvm-tools-preview` timed
out (240 s) due to the same contended/slow network that affected the
toolchain install (Terminated, exit 143). This is an environment/tooling
limitation, recorded honestly rather than estimated. Coverage in the
behavioural sense is nevertheless exercised broadly and deterministically by
the 44 tests above: every public operation, both container kinds and all four
kind pairings, the 4095/4096/4097 boundary, dense/full and sparse extremes,
the 2^32-edge keys, streaming-vs-slice decode, and the hostile-length /
truncation / budget cases all run and pass.

## 7. Reproduce

```bash
cargo build --release
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test --release
./target/release/rbitset examples/encode.json
```
