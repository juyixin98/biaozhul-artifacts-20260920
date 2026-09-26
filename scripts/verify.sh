#!/usr/bin/env bash
# Reproduce the full verification: lint, tests, and the CLI demo workflow.
#
# Usage:
#   scripts/verify.sh                # fmt check + clippy + tests + demo
#   scripts/verify.sh --no-demo      # lint + tests only
#
# Requires a Rust stable toolchain (cargo, rustfmt, clippy).
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> cargo fmt --check"
cargo fmt --all -- --check

echo "==> cargo clippy -D warnings"
cargo clippy --all-targets -- -D warnings

echo "==> cargo test"
cargo test --release

if [[ "${1:-}" == "--no-demo" ]]; then
  exit 0
fi

BIN="target/release/cdict"
echo "==> cargo build --release"
cargo build --release

mkdir -p out
echo "==> CLI workflow"
"$BIN" examples/encode.request.json
"$BIN" examples/decode.request.json
"$BIN" examples/encode2.request.json
"$BIN" examples/merge.request.json
"$BIN" examples/inspect.request.json
echo "-- merged decode --"
"$BIN" - <<<'{"op":"decode","input":"out/merged.cdc","output":"-"}' 2>/dev/null
echo
echo "OK"
