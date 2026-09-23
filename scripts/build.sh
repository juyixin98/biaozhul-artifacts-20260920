#!/usr/bin/env bash
# Build main + test sources with plain javac. No third-party dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p build/classes build/test-classes

echo "== compiling main sources =="
find src/main/java -name '*.java' | sort > build/main-sources.txt
javac -encoding UTF-8 -d build/classes @build/main-sources.txt

echo "== compiling test sources =="
find src/test/java -name '*.java' | sort > build/test-sources.txt
javac -encoding UTF-8 -cp build/classes -d build/test-classes @build/test-sources.txt

echo "== build OK =="
