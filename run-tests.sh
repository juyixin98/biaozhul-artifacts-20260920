#!/usr/bin/env bash
# Build and run the test suite. Requires JDK 17+ (see .java-version).
set -euo pipefail
cd "$(dirname "$0")"

rm -rf out
mkdir -p out/main out/test

javac --release 17 -d out/main $(find src/main/java -name '*.java')
javac --release 17 -cp out/main -d out/test $(find src/test/java -name '*.java')
java -cp out/main:out/test dedup.DedupServiceTest
