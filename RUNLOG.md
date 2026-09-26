# Build & test run log

Record of the commands actually executed and their observed results during
development and acceptance. Times are 2026-09-25 (UTC+host), Linux x86_64.

## 0. Toolchain note (environment problem, worked around)

The machine's default `~/.rustup` stable toolchain was left half-installed
(`error: missing manifest in toolchain 'stable-x86_64-unknown-linux-gnu'`)
because several parallel sessions on the shared host were concurrently
running `rustup toolchain install` into the same `RUSTUP_HOME`, causing
download-file rename races (`could not rename '*.partial' …`).

Workaround (no change to the project): a complete 1.98.1 toolchain was
assembled from the official component archives into `/tmp/ec-toolchain`
(rustc + cargo from a direct resumable download of
`rustc-1.98.1-x86_64-unknown-linux-gnu.tar.xz`, plus the already-complete
`rust-std` and `cargo` archives), and `rustfmt`/`clippy` were added the same
way. All commands below use:

```bash
export PATH=/tmp/ec-toolchain/bin:$PATH CARGO_HOME=/tmp/ec-cargo-home
```

`rustc 1.98.1 (48a229cea 2026-09-01)`, `cargo 1.98.1`.

## 1. Build

```
$ cargo build
    Finished `dev` profile …   (0 warnings)
$ cargo build --release
    Finished `release` profile …   (0 warnings)
```

## 2. Formatting / lint

```
$ cargo fmt --all -- --check
FMT_CLEAN
$ cargo clippy --all-targets -- -D warnings
    Finished `dev` profile …
CLIPPY_EXIT=0    (no warnings)
```

## 3. Automated test suite

```
$ cargo test
  src/lib.rs  unit tests ............ 7 passed, 0 failed
  src/main.rs unit tests ............ 1 passed, 0 failed
  tests/integration.rs .............. 17 passed, 0 failed
  doc-tests ......................... 0 passed, 0 failed
```

Coverage of the acceptance criteria:

| Required case | Where it is exercised | Result |
|---|---|---|
| Enumerate small-parameter loss combinations | `coding::tests::every_k_row_selection_is_invertible` (k=1..5, m=1..3, every k-of-n selection), `mds_at_larger_parameters` (k=8,m=4, all 495 combos), `recover_enumerated_small_loss_patterns`, `enumerated_loss_patterns_small_parameters` | all pass |
| Recover with ≤ m known-missing shards (data/parity/mixed) | the above + `per_stripe_cell_loss_is_supported_and_recovered` + CLI test | pass |
| Too many shards missing | `too_many_missing_shards_is_rejected`, `cli_decode_reports_too_many_missing_as_error_json`, acceptance step 4 (`not_enough_shards`, exit 1) | pass |
| Inconsistent shard length | `coding::tests::length_mismatch_is_rejected`, `inconsistent_shard_length_is_rejected`, acceptance step 4b (`length_mismatch`, exit 1) | pass |
| Silent corruption needs an external check | `silent_corruption_decodes_wrong_without_external_check` (decode succeeds with wrong bytes), `external_hash_detects_silent_corruption` (`hash_mismatch`), acceptance steps 5 & 6 | pass |
| Memory / output-length limits | `memory_limit_is_enforced`, `output_length_limit_is_enforced`, `encoder_enforces_output_and_memory_budgets`, `oversized_chunk_record_is_rejected` | pass |
| Framing damage | `bad_magic_and_truncated_container_are_rejected` | pass |
| Empty input / zero-length final shard | `roundtrip_empty_input`, `zero_length_data_shard_in_final_stripe_is_explicitly_present` | pass |
| JSON control entry point | `cli_encode_decode_info_and_corrupt_via_json` (drives the real binary) | pass |

## 4. End-to-end script

```
$ bash scripts/acceptance.sh release
```

Observed outcomes:

1. **encode** k=3 m=2 S=1024 on 5000 random bytes → `ok`, 2 stripes, SHA-256
   tagged.
2. **info** → header echoed (`data_shards=3`, `total_len=5000`, `sha256`).
3. **decode** with shards 1 and 4 lost on every stripe (exactly m losses) +
   hash verify → `ok`, `cmp` prints `OUTPUT MATCHES INPUT`.
4. **decode** with shards 0,1,2 lost (3 > m=2) →
   `{"status":"error","error":{"kind":"not_enough_shards", …}}`, exit code 1.
4b. **decode** a container whose first data payload was shortened by one byte
   → `{"kind":"length_mismatch", … "got 1023 bytes, expected 1024"}`,
   exit code 1.
5. flip one byte of parity shard 3, lose data shard 0, decode **without**
   hash verification → `ok` but output differs from input
   (`DECODED DATA IS WRONG …`). This demonstrates Reed-Solomon cannot detect
   silent corruption by itself.
6. the same reconstruction **with** `expect_hash:true` →
   `{"kind":"hash_mismatch", …}`, exit code 1 — the external SHA-256 detects
   the corruption.
7. full `cargo test` → 25 tests pass (7 lib + 1 bin + 17 integration),
   0 failures.

## 5. Design change made during verification

The parity block was first written as a Vandermonde matrix `a_p^j`. Static
review (before the toolchain was available) showed this layout is **not**
MDS on GF(2⁸): a mixed data/parity survivor sub-matrix is a generalized
Vandermonde minor whose exponent set can make the determinant zero
(e.g. k=4, parity points 1,2,3, missing columns {0,1,3}:
det ∝ (1+2+3) = 0). It was replaced by a Cauchy block
`1/(x_p + y_j)` with disjoint point sets, which is provably MDS: every
square submatrix is Cauchy and nonsingular. The exhaustive enumeration tests
guard this property.
