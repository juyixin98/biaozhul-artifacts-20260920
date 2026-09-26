# RUNLOG — build & acceptance record

This file records the **actual** commands run and their observed results while
developing and verifying the project, including failures encountered and items
that could not be run in this environment. Nothing here is aspirational.

- Date: 2026-09-25
- Host: Linux x86_64 (kernel 6.8.0-90-generic, Ubuntu)
- Toolchain used for all recorded results: **rustc 1.75.0 / cargo 1.75.0**
  (system toolchain at `/usr/bin`).
- The project has **no third-party dependencies** (`cargo build` performs no
  network access).

## Environment note (toolchain)

`rustup default stable` was attempted first. Multiple concurrent rustup
install processes from other sessions shared `~/.rustup` and repeatedly
corrupted each other's partial downloads ("bad checksum for cached download",
"missing manifest in toolchain"). Rather than contend for the shared
directory, all builds/tests here use the preinstalled system
`/usr/bin/cargo` 1.75.0. The source uses only stable, edition-2021 features
also valid on current stable (1.98.1 was observed installing). `rustfmt`
1.7.0 was run on every source file, and **clippy is clean with
`cargo clippy --all-targets -- --deny warnings`** using the standalone
`/usr/bin/cargo-clippy` (which bypasses the contended rustup shim).

## Build

Command:

```sh
cargo build --release
```

Result: **success**, zero compiler warnings.

```
Compiling aac v0.1.0 (...)
Finished release [optimized] target(s)
```

`cargo build` (debug) also reports **0** warnings.

## Unit + integration tests

Command:

```sh
cargo test --release
```

Result: **all pass**.

```
running 15 tests  (src unit tests: bitio, model, jsonutil, container)
test result: ok. 15 passed; 0 failed; 0 ignored

running 19 tests  (tests/codec_test.rs end-to-end)
test result: ok. 19 passed; 0 failed; 0 ignored

doc-tests: 0
```

Total **34 automated tests, 0 failures**.

Coverage of the requested acceptance points inside the test suite:

| Requirement                                   | Test(s) |
|-----------------------------------------------|---------|
| empty string                                  | `empty_input_roundtrips`, `exhaustive_short_binary_sequences` (len 0) |
| every byte (0–255)                            | `each_single_byte_roundtrips`, `all_byte_values_once_and_repeated` |
| long skewed data → multiple rescales          | `long_skewed_data_triggers_many_rescales` (asserts ≥20; observed 23), `skewed_runs_of_every_symbol_rescale` |
| truncated streams                             | `every_truncated_prefix_is_rejected` (every cut), `every_truncated_prefix_rejected_via_stream`, `raw_coded_payload_truncated_decoder_errors_on_short_prefixes` |
| determinism                                   | `output_is_deterministic` (identical bytes on repeated encode) |
| corruption / format validation                | `corrupted_byte_anywhere_is_rejected`, `non_zero_padding_bits_rejected`, `forged_length_field_above_cap_rejected`, `empty_or_garbage_container_rejected`, container unit tests |
| length limits                                 | `decode_output_limit_is_enforced`, `encode_payload_limit_is_enforced` |
| stream vs buffered equivalence                | `streaming_and_buffered_containers_are_identical`, stream round-trips |
| extra correctness confidence                  | 40 seeded random fuzz round-trips; exhaustive binary sequences up to length 9; exhaustive ternary sequences up to length 5 |

## End-to-end CLI acceptance

Command:

```sh
CARGO=/usr/bin/cargo ./scripts/acceptance.sh
```

Result: **12 passed, 0 failed**.

```
== build ==
PASS  release build
== round-trips ==
PASS  empty string round-trip
PASS  all 256 byte values round-trip
PASS  long skewed data round-trip
== rescaling ==
    skewed rescales = 23
PASS  skewed data triggers multiple rescales (>=10)
== determinism ==
PASS  output is deterministic
== truncation ==
PASS  all sampled truncated prefixes rejected
== JSON control entry ==
PASS  json encode empty succeeds
PASS  json encode/decode round-trips 'hello'
PASS  json decode error envelope on bad input
== output cap ==
PASS  decode output-length cap enforced
== bounded memory on a 50 MB stream ==
    peak RSS encode = 2176 KiB for 50 MB input
PASS  50 MB stream round-trips with peak RSS < 10 MiB

RESULT: 12 passed, 0 failed
```

### Observed codec figures (real runs)

| Input | Source bytes | Container bytes | Payload bytes | Coded bits | Rescales |
|-------|-------------:|----------------:|--------------:|-----------:|---------:|
| empty | 0 | 35 | 6 | 42 | 0 |
| `bytes(0..256) × 4` | 1024 | 1099 | 1070 | 8558 | 0 |
| 200 KB, 99.5% zero bytes | 200000 | 2732 | 2703 | 21623 | 23 |
| `'the quick brown fox ' × 2000` | 40000 | 19462 | 19433 | 155461 | 3 |
| 50 MB of byte `A` | 50000000 | 196379 | 196350 | 1570800 | 6199 |

Container size is always `5 prefix + payload + 24 trailer`; the smallest real
file (empty input) is therefore 35 bytes. High-entropy data (all 256 symbols
evenly) expands slightly, which is expected for an adaptive order-0 coder on
incompressible data.

### JSON control entry (real requests/responses)

Sample files live in `examples-requests/`; the committed `*.response.json`
files were captured from the binary, not hand-written. Examples:

```sh
$ echo '{"op":"encode","data":""}' | aac json
{"ok":true,"op":"encode","result":"QUMwMQH/QAAAAAAAAAAAAAAAKgAAAAAAAAAACGXoPEFDRUQ=",
 "stats":{"input_bytes":0,"output_bytes":6,"coded_bits":42,"rescales":0,"symbols":1}}

$ echo '{"op":"decode","data":"QUMwMQE="}' | aac json ; echo $?
{"ok":false,"op":"decode","error":"InvalidFormat",
 "message":"invalid coded format: file too short: 5 bytes, need at least 29"}
1
```

## Bugs found and fixed during development (truthful record)

1. **Incorrect renormalization loop (functional bug).** The first
   implementation added an early exit `if high - low + 1 >= MIN_RANGE { break }`
   inside the E1/E2/E3 loop. That is not part of the standard integer coder
   and desynchronized encoder/decoder state. Non-empty inputs happened to
   round-trip, but the **empty input failed** with `UnexpectedEnd`
   (decoder consumed virtual zero bits, `symbols=4` on a one-symbol stream).
   Fix: use only the three E1/E2/E3 conditions with the `else break`, on both
   sides. After the fix empty input decodes consuming 0 virtual bits.
2. **Truncated raw payload could decode unbounded garbage** until the 1 GiB
   output cap, hanging the test suite. Fix: after every decoded symbol, abort
   with `UnexpectedEnd` as soon as the decoder touches the virtual all-zero
   tail — a valid stream never does (encoder appends a flush + 32 zero tail
   bits). Truncation now fails at the first missing real bit.
3. **CLI only accepted `--input`, not `-i`.** Fixed the argument parser to
   support both long and short options.
4. A pre-existing **1.75-incompatible `Cargo.lock` (v4)** generated by a newer
   cargo was removed; cargo 1.75 regenerated a compatible lockfile
   (`Cargo.lock` is git-ignored since there are no dependencies to pin).

## Not run / known limitations

- **`cargo clippy` through rustup**: the `cargo clippy`/`cargo fmt` *shims*
  on PATH route through the contended rustup install and time out, so they
  were not used. Linting was instead run successfully with the standalone
  `/usr/bin/cargo-clippy --all-targets -- --deny warnings` (clean, exit 0),
  and formatting with `/usr/bin/rustfmt`. Debug and release builds are
  warning-free under rustc 1.75.
- **Coverage percentage**: no `cargo-llvm-cov`/coverage toolchain is installed
  offline, so an 80% line-coverage figure was not measured. Test breadth
  (34 tests including exhaustive short-sequence enumeration and fuzzing) is
  offered instead; coverage tooling can be added when a registry/network is
  available.
- CRC-32 is an integrity check against accidental truncation/corruption, not
  cryptographic authentication.
- Only an order-0 adaptive model is implemented (as specified); higher-order
  modelling is out of scope.
