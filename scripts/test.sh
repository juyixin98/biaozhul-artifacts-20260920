#!/usr/bin/env bash
# 编译并运行全部自动化测试。仅需 JDK 17+。
set -euo pipefail
cd "$(dirname "$0")/.."
./scripts/build.sh --with-tests

find_java() {
  if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/java" ]; then echo "$JAVA_HOME/bin/java"; return; fi
  if command -v java >/dev/null 2>&1; then command -v java; return; fi
  local cand
  cand=$(ls -d .tools/jdk-*/bin/java 2>/dev/null | sort -V | tail -1 || true)
  echo "$cand"
}

JAVA_BIN="$(find_java)"
[ -n "$JAVA_BIN" ] || { echo "ERROR: java not found" >&2; exit 1; }

$JAVA_BIN -cp build/classes:build/classes-test test.Tests
