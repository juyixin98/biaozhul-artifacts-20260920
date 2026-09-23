#!/usr/bin/env bash
# Compile (if needed) and run the whole automated test suite with the JDK only.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p build/classes build/test-classes

find src/main/java -name '*.java' > build/main-sources.txt
find src/test/java -name '*.java' > build/test-sources.txt
javac -d build/classes @build/main-sources.txt
javac -cp build/classes -d build/test-classes @build/test-sources.txt

echo "=============================================="
echo " Running test suites (main + test classpath)"
echo "=============================================="
CP="build/classes:build/test-classes"
java -cp "$CP" qsummary.GKCoreTest
java -cp "$CP" qsummary.GKMergeTest
java -cp "$CP" qsummary.JsonTest
java -cp "$CP" qsummary.HttpIT
echo "=============================================="
echo " ALL TEST SUITES PASSED"
echo "=============================================="
