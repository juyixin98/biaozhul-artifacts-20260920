#!/usr/bin/env bash
# Compiles all main and test sources with the plain JDK (no Maven/Gradle needed).
set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf target/classes target/test-classes
mkdir -p target/classes target/test-classes

echo "==> compiling main sources"
find src/main/java -name '*.java' > target/main-sources.txt
javac -d target/classes @target/main-sources.txt

echo "==> compiling test sources"
find src/test/java -name '*.java' > target/test-sources.txt
javac -cp target/classes -d target/test-classes @target/test-sources.txt

echo "BUILD OK"
