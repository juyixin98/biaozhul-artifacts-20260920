#!/usr/bin/env bash
# Compile main and test sources with plain javac (no build tool dependency).
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p build/classes build/test-classes

echo "== compiling main sources =="
find src/main/java -name '*.java' > build/main-srcs.txt
javac --release 17 -d build/classes @build/main-srcs.txt

echo "== compiling test sources =="
find src/test/java -name '*.java' > build/test-srcs.txt
javac --release 17 -cp build/classes -d build/test-classes @build/test-srcs.txt

echo "Build OK -> build/classes, build/test-classes"
