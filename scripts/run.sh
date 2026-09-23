#!/usr/bin/env bash
# Start the service. Options after "--" are forwarded to the JVM main class:
#   scripts/run.sh -- --port 9090 --max-in-flight 16
set -euo pipefail
cd "$(dirname "$0")/.."

JAVA="${JAVA:-java}"
JAVAC="${JAVAC:-javac}"
OUT=target/classes

if [ ! -d "$OUT" ] || [ -z "$(find "$OUT" -name '*.class' -print -quit 2>/dev/null)" ]; then
  mkdir -p "$OUT"
  find src/main/java -name '*.java' > target/sources.txt
  "$JAVAC" -encoding UTF-8 -d "$OUT" @target/sources.txt
fi

exec "$JAVA" -cp "$OUT" orderedevents.Main "$@"
