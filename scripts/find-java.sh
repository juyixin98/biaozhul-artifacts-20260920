#!/usr/bin/env bash
# 定位可用的 JDK 17+：优先 $JAVA_HOME，其次 /home/admin/tools 下的 JDK，最后 PATH 中的 javac。
# 导出 JAVA_BIN（javac/java 所在目录）。
set -euo pipefail

candidate_ok() {
  local bin="$1"
  [ -x "$bin/javac" ] && [ -x "$bin/java" ] || return 1
  local v
  v=$("$bin/javac" -version 2>&1 | sed -E 's/.* ([0-9]+)(\..*)?/\1/')
  [ "$v" -ge 11 ] 2>/dev/null
}

if [ -n "${JAVA_HOME:-}" ] && candidate_ok "$JAVA_HOME/bin"; then
  JAVA_BIN="$JAVA_HOME/bin"
else
  for d in /home/admin/tools/jdk-17* "$HOME"/tools/jdk-17* /usr/lib/jvm/java-17* /usr/lib/jvm/java-21* /usr/lib/jvm/java-11*; do
    [ -d "$d" ] || continue
    if candidate_ok "$d/bin"; then
      JAVA_BIN="$d/bin"
      break
    fi
  done
  if [ -z "${JAVA_BIN:-}" ]; then
    if command -v javac >/dev/null 2>&1 && command -v java >/dev/null 2>&1; then
      JAVA_BIN="$(dirname "$(command -v javac)")"
    else
      echo "ERROR: no JDK 11+ found. Set JAVA_HOME or extract a JDK tarball." >&2
      exit 2
    fi
  fi
fi

echo "$JAVA_BIN"
