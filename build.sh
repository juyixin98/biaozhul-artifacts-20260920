#!/usr/bin/env bash
# Compile library + tests. No network access, no external dependencies.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p build/classes build/test-classes

echo "== compiling library =="
javac -encoding UTF-8 -d build/classes $(find src -name '*.java')

echo "== compiling tests =="
javac -encoding UTF-8 -cp build/classes -d build/test-classes \
    $(find tests -name '*.java')

echo "== build OK =="
