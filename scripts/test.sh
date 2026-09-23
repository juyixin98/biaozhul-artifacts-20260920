#!/usr/bin/env bash
# Run all automated tests. Builds first, so this is the single CI entry point.
set -euo pipefail
cd "$(dirname "$0")/.."

scripts/build.sh >/tmp/dedup-build.log 2>&1 || {
  cat /tmp/dedup-build.log
  echo "BUILD FAILED" >&2
  exit 1
}

if [ -z "${JAVA_HOME:-}" ]; then
  if [ -x "$HOME/jdk17/usr/lib/jvm/java-17-openjdk-amd64/bin/java" ]; then
    JAVA_HOME="$HOME/jdk17/usr/lib/jvm/java-17-openjdk-amd64"
  fi
fi
JAVA="${JAVA_HOME:+$JAVA_HOME/bin/}java"

"$JAVA" -cp build/classes:build/test-classes dedup.test.TestRunner \
  dedup.test.JsonTest \
  dedup.test.DedupCoreTest \
  dedup.test.HttpApiTest
