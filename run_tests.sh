#!/usr/bin/env bash
# run_tests.sh — 一键运行全部自动化测试（纯 Rust 单测 + 真实 TCP 集成测试）
# 以及（可选）用 scripts/ws_probe.py 对真实起的服务做端到端探针。
set -euo pipefail
cd "$(dirname "$0")"

echo "################################################################"
echo "# 1) cargo test：库内单元测试 + 真实 TCP 集成测试"
echo "################################################################"
cargo test

if [[ "${1:-}" == "--probe" ]]; then
  echo
  echo "################################################################"
  echo "# 2) 构建 release 并用 stdlib Python 探针做端到端验收"
  echo "################################################################"
  cargo build --release
  BIN=./target/release/wsecho
  PORT=9091
  "$BIN" --addr "127.0.0.1:${PORT}" --raw --quiet >/tmp/wsecho_probe.log 2>&1 &
  SRV=$!
  trap 'kill "$SRV" 2>/dev/null || true' EXIT
  sleep 0.5

  pass=0; fail=0
  for c in echo ping-insert illegal-cont double-start half-utf8-valid \
           half-utf8-truncated bad-utf8-byte too-big control-too-long \
           reserved-opcode unmasked close-codes reserved-close-code; do
    if python3 scripts/ws_probe.py --raw --port "$PORT" --case "$c" --byte-by-byte \
           >"/tmp/probe_${c}.log" 2>&1; then
      printf 'PASS  %s\n' "$c"; pass=$((pass+1))
    else
      printf 'FAIL  %s (see /tmp/probe_%s.log)\n' "$c" "$c"; fail=$((fail+1))
    fi
  done
  echo "----------------------------------------------------------------"
  echo "probe result: ${pass} passed, ${fail} failed"
  [[ "$fail" -eq 0 ]]
fi
