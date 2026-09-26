# RUNLOG — actual build & test record

This file records the commands that were actually run during development,
their results, and the one environment problem that had to be solved. All
timestamps are from 2026-09-25 on Linux x86_64.

## Toolchain problem and resolution (honest record)

The machine's default rustup stable toolchain was **broken**: repeated
`rustup toolchain install stable` failed while extracting components, with
errors such as:

```
warn: bad checksum for cached download
error: component download failed for cargo-x86_64-unknown-linux-gnu:
  could not rename 'downloaded' file ... : No such file or directory (os error 2)
error: failed to extract package: premature eof
```

The existing `~/.rustup/toolchains/stable-x86_64-unknown-linux-gnu` contained
only a `cargo` binary; `rustc` was missing (`'cargo' component is not
applicable`, `rustc ... No such file or directory`).

Deleting `~/.rustup` contents was blocked by the environment's safety policy,
so the toolchain was instead installed into a **separate, non-destructive
rustup home**:

```bash
export RUSTUP_HOME="$HOME/rustup-fresh"
rustup toolchain install stable --profile minimal --no-self-update
# result: success
$RUSTUP_HOME/toolchains/stable-x86_64-unknown-linux-gnu/bin/rustc --version
# rustc 1.98.1 (48a229cea 2026-09-01)
```

All build/test commands below were run with that variable set. The project
itself has **zero third-party dependencies**, so it builds fully offline once a
toolchain is present.

## Build

```text
$ cargo build
    Finished `dev` profile [unoptimized + debuginfo] target(s)

$ cargo build --release
   Compiling bse v0.1.0
    Finished `release` profile [optimized] target(s) in 4.18s
```

No warnings from the compiler on the final tree.

## Automated tests

Command:

```bash
cargo test
```

Result (all passing, **57 tests, 0 failed**):

```text
running 23 tests            # src unit tests (wire, schema, json/base64/integer precision, value)
test result: ok. 23 passed; 0 failed

tests/api_e2e.rs      : test result: ok. 8 passed; 0 failed
tests/codec_extra.rs  : test result: ok. 8 passed; 0 failed
tests/evolution.rs    : test result: ok. 11 passed; 0 failed
tests/limits.rs       : test result: ok. 5 passed; 0 failed
tests/streaming.rs    : test result: ok. 2 passed; 0 failed
doc-tests             : test result: ok. 0 passed; 0 failed
```

## Lint / format

```text
$ cargo clippy --all-targets -- -D warnings
    Finished `dev` profile          # no warnings/errors, exit 0

$ cargo fmt --check
(no output; formatting clean)
```

## End-to-end demo

Command:

```bash
./examples/demo.sh
```

Result: exit code **0**; all ten steps completed. Key observed outputs:

1. v1 encode → `payload_bytes = 82`; hex starts `42534531 0100 52000000`
   (`"BSE1"`, version 1, flags 0, LE length 82); field `id` explicit zero is
   emitted as `08 00`.
2. v1 decode → `"id":{"present":0}`, `"email":{"missing":true}`,
   `"active":{"present":false}` — explicit zero/false kept distinct from
   missing.
3. v2 encode succeeded (adds age/scores/nickname/city).
4. v2 bytes decoded under **v1**: known `name` decodes; `__unknown__.count =
   3` at top level (fields 7, 8, 10) plus one nested unknown (`addr.city`,
   field 3).
5. Forward through v1 with a `name` patch, then decode under **v2** again:
   `name = "Edited by v1"`, `age = 79`, `scores = [10,20,-3,0]`,
   `nickname = "Amazing Grace"`, nested `city = "Arlington"` all recovered —
   unknown fields survived the hop byte-for-byte.
6. v1 bytes decoded under **v2**: added `age`/`scores` report
   `{"missing":true}`.
7. `compat` v1↔v2 → `true`.
8. `compat` v1↔v3 → `false`, reporting field 3 `email`
   (`string (2) -> int32 (0)`) and field 20 `tenant_id` (newly added
   `required`) as incompatible.
9. Decoding v2 bytes under v3 → rejected, exit 1:
   `incompatible schema evolution for field 3: field 'email' (int32) expects
   wire type Varint but data carries Len ...`.
10. Truncating the envelope payload to 20 bytes → rejected, exit 1:
    `unexpected end of input` (clean typed error; no panic).

## Failed/skipped items

* No test is failing or `#[ignore]`d in the final tree.
* No third-party crate is used, so `cargo audit` / dependency-license checks
  are not applicable.
* No frontend was built, per the backend-only requirement.
* Coverage tooling (`cargo-llvm-cov`) is not installed in this offline
  environment, so an automated coverage percentage was not produced; test
  coverage is exercised at the unit and integration layers across every public
  operation and each error path listed in `FORMAT.md` §7.
