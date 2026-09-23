#!/usr/bin/env bash
# Compile and run the dependency-free end-to-end test suite.
set -euo pipefail
cd "$(dirname "$0")/.."

JAVA="${JAVA:-java}"
JAVAC="${JAVAC:-javac}"

mkdir -p target/classes target/test-classes
find src/main/java -name '*.java' > target/sources.txt
find src/test/java -name '*.java' > target/test-sources.txt
"$JAVAC" -encoding UTF-8 -d target/classes @target/sources.txt
"$JAVAC" -encoding UTF-8 -cp target/classes -d target/test-classes @target/test-sources.txt
"$JAVA" -cp target/classes:target/test-classes orderedevents.OrderedEventsHttpTest
