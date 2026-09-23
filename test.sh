#!/usr/bin/env bash
# Compile and run the automated test suite with plain javac/java.
set -euo pipefail
cd "$(dirname "$0")"
source ./scripts-common.sh

mkdir -p target/classes target/test-classes
find src/main/java -name '*.java' > target/sources.txt
find src/test/java -name '*.java' > target/test-sources.txt
"$JAVAC" -Xlint:all -d target/classes @target/sources.txt
"$JAVAC" -Xlint:all -cp target/classes -d target/test-classes @target/test-sources.txt
exec "$JAVA" -cp target/classes:target/test-classes io.example.orderedcommit.TestRunner
