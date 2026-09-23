#!/usr/bin/env bash
# Compile main and test sources with plain javac (no build tool, no deps).
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p target/classes target/test-classes
find src/main/java -name '*.java' > target/main-sources.txt
javac -d target/classes @target/main-sources.txt
find src/test/java -name '*.java' > target/test-sources.txt
javac -cp target/classes -d target/test-classes @target/test-sources.txt
echo "BUILD OK -> target/classes, target/test-classes"
