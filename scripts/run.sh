#!/usr/bin/env bash
# Start the HTTP service. Usage: scripts/run.sh [port]   (default 8080)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d build/classes ]; then
  scripts/build.sh
fi

if [ -z "${JAVA_HOME:-}" ] && [ -x "$HOME/jdk17/usr/lib/jvm/java-17-openjdk-amd64/bin/java" ]; then
  JAVA_HOME="$HOME/jdk17/usr/lib/jvm/java-17-openjdk-amd64"
fi
JAVA="${JAVA_HOME:+$JAVA_HOME/bin/}java"

exec "$JAVA" -cp build/classes dedup.Main "$@"
