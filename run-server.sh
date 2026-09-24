#!/usr/bin/env bash
# Start the dedup server. Requires JDK 17+ (see .java-version).
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p out/main
javac --release 17 -d out/main $(find src/main/java -name '*.java')
# Extra args are passed through, e.g.:
#   ./run-server.sh --port=8080 --partitions=16 --allowed-lateness-ms=60000 --retention-ms=300000
java -cp out/main dedup.DedupServer "$@"
