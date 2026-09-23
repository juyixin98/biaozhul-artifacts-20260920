#!/usr/bin/env bash
# Build (if needed) and start the HTTP service.
set -euo pipefail
cd "$(dirname "$0")"
source ./scripts-common.sh

JAR=target/ordered-events.jar
if [[ ! -f "$JAR" ]]; then
  ./build.sh
fi

exec "$JAVA" ${JAVA_OPTS:-} -jar "$JAR" "$@"
