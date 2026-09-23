#!/usr/bin/env bash
# Acceptance script for the verifiable-computation-receipt demo.
# Runs BOTH the fast test suite and real end-to-end CLI scenarios.
# Real STARK proofs take several seconds each on CPU.
set -u
cd "$(dirname "$0")/.."

BIN="cargo run --release --"
pass=0
fail=0

# expect_ok <description> -- <command...>
expect_ok() {
  local desc="$1"; shift
  [ "$1" = "--" ] && shift
  if "$@" >/tmp/vcr_out.log 2>&1; then
    echo "PASS  $desc"; pass=$((pass+1))
  else
    echo "FAIL  $desc (expected success)"; tail -5 /tmp/vcr_out.log; fail=$((fail+1))
  fi
}

# expect_fail <description> -- <command...>
expect_fail() {
  local desc="$1"; shift
  [ "$1" = "--" ] && shift
  if "$@" >/tmp/vcr_err.log 2>&1; then
    echo "FAIL  $desc (expected failure)"; tail -5 /tmp/vcr_err.log; fail=$((fail+1))
  else
    echo "PASS  $desc"; pass=$((pass+1))
  fi
}

echo "==> Building release"
cargo build --release >/tmp/vcr_build.log 2>&1 || { echo "build failed"; tail -20 /tmp/vcr_build.log; exit 1; }
B=./target/release/vcr-host
mkdir -p receipts

echo; echo "==> Fast automated tests (dev-mode execution + security logic)"
cargo test --release >/tmp/vcr_tests.log 2>&1 \
  && echo "PASS  fast suite (15 tests)" && pass=$((pass+1)) \
  || { echo "FAIL  fast suite"; grep -E "FAILED|panicked" /tmp/vcr_tests.log | head; fail=$((fail+1)); }

echo; echo "==> Real proof scenarios"
expect_ok  "real median proof verifies"                -- $B demo --receipt receipts/a.bin
expect_ok  "independently verify saved receipt"       -- $B verify --receipt receipts/a.bin --bind inputs/example.json
expect_ok  "real proof for decoy program"             -- $B demo --guest decoy --receipt receipts/d.bin
expect_ok  "decoy receipt verifies under decoy ID"    -- $B verify --receipt receipts/d.bin --bind inputs/example.json --expect-decoy
expect_ok  "real proof over 256 elements"             -- $B prove --input inputs/max_batch_256.json --receipt receipts/m.bin

echo; echo "==> Negative scenarios (all must be rejected)"
expect_fail "wrong program ID (decoy receipt vs median ID)" -- $B verify --receipt receipts/d.bin --bind inputs/example.json
expect_fail "tampered journal"                              -- $B verify --receipt receipts/a.bin --bind inputs/example.json --tamper-journal
expect_fail "input commitment mismatch (different batch)"   -- $B verify --receipt receipts/a.bin --bind inputs/six_values_even_len.json
expect_fail "dev-mode fake receipt refused"                 -- $B demo --dev-mode --receipt receipts/f.bin
expect_fail "fake receipt refused even when RISC0_DEV_MODE=1 is set" \
                                                            -- env RISC0_DEV_MODE=1 $B demo --dev-mode --receipt receipts/f2.bin
expect_fail "out-of-range input"                            -- $B prove --input inputs/out_of_range.json
expect_fail "empty batch"                                   -- $B prove --input inputs/empty.json

echo; echo "=================================================="
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
