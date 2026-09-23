#!/usr/bin/env bash
# Optional ordering stress check: 100 random-delay events, asserts gapless,
# exactly-once, per-seq id/status correctness over real HTTP.
set -euo pipefail
cd "$(dirname "$0")/.."
JAVA="${JAVA:-java}"
JAVAC="${JAVAC:-javac}"
mkdir -p target/perf
find src/main/java -name '*.java' > target/sources.txt
"$JAVAC" -encoding UTF-8 -d target/classes @target/sources.txt
"$JAVAC" -encoding UTF-8 -cp target/classes -d target/perf src/perf/java/Stress.java
"$JAVA" -cp target/classes:target/perf Stress
