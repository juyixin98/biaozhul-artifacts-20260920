#!/usr/bin/env bash
# Compiles and runs the JDK-only test suite. Exit code != 0 on any failure.
set -euo pipefail
cd "$(dirname "$0")"

./build.sh

# shellcheck source=scripts/resolve-jdk.sh
source scripts/resolve-jdk.sh

mkdir -p build/test-classes
find src/test/java -name '*.java' > build/test-sources.txt
"$JAVAC" -encoding UTF-8 -cp build/classes -d build/test-classes @build/test-sources.txt
rm -f build/test-sources.txt
echo "---- running tests ----"
"$JAVA" -cp "build/classes:build/test-classes" com.example.window.TestMain
